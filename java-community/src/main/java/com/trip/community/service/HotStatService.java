package com.trip.community.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.trip.community.entity.HotStat;
import com.trip.community.mapper.HotStatMapper;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * 热度统计 —— **反哺 Python 生成**的数据来源（这是"闭环"里最值钱的一段）。
 *
 * <p>它回答的问题：现在大家最爱去哪些城市、最常选哪些偏好标签？
 * Python 生成行程前会读它（{@code GET /hot}），把这些作为默认偏好或候选加权，
 * 于是"用户行为 → 热度 → 下一次生成"形成闭环，而不是各干各的。
 *
 * <p>为什么用 MySQL 表而不是纯 Redis：
 * 热度是**低频更新、需要长期累积**的统计量（今天热什么，不该因为 Redis 重启就归零）。
 * 高频计数在 Redis，落成统计结果放数据库 —— 两类数据的生命周期不同，不该混在一处。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class HotStatService {

    private final HotStatMapper hotStatMapper;
    private final JdbcTemplate jdbcTemplate;

    /**
     * 发布一条行程时累加热度（城市 + 偏好标签 + 景点）。
     *
     * <p>偏好为什么必须单独传进来：行程 JSON 里只有排好的每天去哪，
     * **不含用户当初勾的偏好标签**（那是请求的信息）。而"大家最爱选什么偏好"
     * 恰恰是反哺生成时最有用的那一路数据 —— 所以由调用方显式传。
     */
    public void accumulatePublish(String city, JsonNode plan, List<String> preferences) {
        if (city != null && !city.isBlank()) {
            upsert("city", city, 1);
        }
        if (preferences != null) {
            for (String preference : preferences) {
                if (preference != null && !preference.isBlank()) {
                    upsert("preference", preference.trim(), 1);
                }
            }
        }
        JsonNode days = plan == null ? null : plan.get("days");
        if (days != null && days.isArray()) {
            for (JsonNode day : days) {
                JsonNode spots = day.get("attractions");
                if (spots == null || !spots.isArray()) {
                    continue;
                }
                for (JsonNode spot : spots) {
                    JsonNode name = spot.get("name");
                    if (name != null && !name.asText().isBlank()) {
                        upsert("attraction", name.asText(), 1);
                    }
                }
            }
        }
    }

    /** 累加式写入：存在就 +delta，不存在就插入。 */
    public void upsert(String type, String key, int delta) {
        hotStatMapper.upsertScore(type, key, delta);
    }

    /**
     * 读热度 Top N。这是 Java 暴露给 Python 的接口数据源。
     *
     * @param type city / preference / attraction；为空则返回三类合并
     */
    public Map<String, List<Map<String, Object>>> top(int limit, String type) {
        Map<String, List<Map<String, Object>>> result = new LinkedHashMap<>();
        if (type == null || type.isBlank()) {
            for (String kind : List.of("city", "preference", "attraction")) {
                result.put(kind, queryTop(kind, limit));
            }
        } else {
            result.put(type, queryTop(type, limit));
        }
        return result;
    }

    private List<Map<String, Object>> queryTop(String type, int limit) {
        QueryWrapper<HotStat> query = new QueryWrapper<HotStat>()
                .eq("stat_type", type)
                .orderByDesc("score")
                .last("LIMIT " + Math.max(1, Math.min(limit, 50)));
        List<Map<String, Object>> items = new ArrayList<>();
        for (HotStat stat : hotStatMapper.selectList(query)) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("key", stat.getStatKey());
            item.put("score", stat.getScore());
            items.add(item);
        }
        return items;
    }

    /**
     * 从已发布行程**全量重算**城市热度。
     *
     * <p>为什么要有重算：累加式统计会随"重复执行/重放"漂移，重算是幂等的纠正手段
     * （和点赞数用流水重算是同一个思路）。
     */
    public void recalcCityFromItineraries() {
        int updated = jdbcTemplate.update("""
                INSERT INTO tb_hot_stat (stat_type, stat_key, score)
                SELECT 'city', city, COUNT(*) FROM tb_itinerary WHERE status = 1 GROUP BY city
                ON DUPLICATE KEY UPDATE score = VALUES(score)
                """);
        log.info("城市热度已从行程表重算 | 影响 {} 行", updated);
    }
}
