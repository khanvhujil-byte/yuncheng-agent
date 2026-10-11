package com.trip.community.service;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.stereotype.Service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.trip.community.entity.Itinerary;
import com.trip.community.mapper.ItineraryMapper;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * 行程发布与查询。
 *
 * <p>发布是**跨语言联动的入口**：Python 生成的 {@code TripPlan} 原样交给本服务入库，
 * Java 不解析行程内部结构（只取 city / travel_days 这类索引字段）。
 * 为什么不解析：行程结构演进交给 Python 那一侧，Java 只在需要索引/统计时读少数字段 ——
 * 这样 Python 改契约不会把 Java 改崩。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ItineraryService {

    private final ItineraryMapper itineraryMapper;
    private final LikeService likeService;
    private final HotStatService hotStatService;
    private final AiClientService aiClient;
    private final ObjectMapper objectMapper = new ObjectMapper();

    /**
     * 发布一条行程。
     *
     * @param plan Python 生成的 TripPlan（原样存 JSON）
     * @return 新行程的 id
     */
    public long publish(long userId, String title, String city, Integer travelDays,
                        String startDate, JsonNode plan) {
        Itinerary itinerary = new Itinerary();
        itinerary.setUserId(userId);
        itinerary.setTitle(title == null || title.isBlank() ? city + "行程" : title);
        itinerary.setCity(city);
        itinerary.setTravelDays(travelDays == null ? 1 : travelDays);
        if (startDate != null && !startDate.isBlank()) {
            try {
                itinerary.setStartDate(LocalDate.parse(startDate));
            } catch (Exception e) {
                log.warn("start_date 解析失败，按空处理 | {}", startDate);
            }
        }
        try {
            itinerary.setPlanJson(objectMapper.writeValueAsString(plan));
        } catch (Exception e) {
            throw new IllegalArgumentException("plan 不是合法 JSON：" + e.getMessage(), e);
        }
        itinerary.setAiSummary(aiClient.generateSummary(city, itinerary.getTravelDays(), itinerary.getPlanJson()));
        itinerary.setLikeCount(0);
        itinerary.setViewCount(0);
        itinerary.setStatus(1);
        itinerary.setCreatedAt(LocalDateTime.now());
        itinerary.setUpdatedAt(LocalDateTime.now());

        itineraryMapper.insert(itinerary);
        long id = itinerary.getId();

        // 预热 Redis：让点赞路径第一次就命中 meta（省一次 DB 查询），同时防穿透
        likeService.warmMeta(id, city,
                itinerary.getCreatedAt() == null ? "" : itinerary.getCreatedAt().toString());
        // 热度统计：给 Python 反哺用（"哪些城市/偏好正在变热"）
        hotStatService.accumulatePublish(city, plan);

        log.info("行程已发布 | id={} city={} days={} summary={}字",
                id, city, itinerary.getTravelDays(),
                itinerary.getAiSummary() == null ? 0 : itinerary.getAiSummary().length());
        return id;
    }

    public Map<String, Object> detail(long id, long userId) {
        Itinerary itinerary = itineraryMapper.selectById(id);
        if (itinerary == null) {
            return null;
        }
        Map<String, Object> result = toBrief(itinerary);
        result.put("plan", readPlan(itinerary.getPlanJson()));
        result.put("likeCount", likeService.likeCount(id));
        result.put("hasLiked", likeService.hasLiked(id, userId));
        return result;
    }

    public List<Map<String, Object>> listByCity(String city, int limit) {
        QueryWrapper<Itinerary> query = new QueryWrapper<Itinerary>()
                .eq("status", 1)
                .orderByDesc("created_at")
                .last("LIMIT " + Math.max(1, Math.min(limit, 50)));
        if (city != null && !city.isBlank()) {
            query.eq("city", city);
        }
        return itineraryMapper.selectList(query).stream()
                .map(item -> {
                    Map<String, Object> brief = toBrief(item);
                    brief.put("likeCount", likeService.likeCount(item.getId()));
                    return brief;
                })
                .toList();
    }

    private Map<String, Object> toBrief(Itinerary itinerary) {
        Map<String, Object> brief = new LinkedHashMap<>();
        brief.put("itineraryId", itinerary.getId());
        brief.put("title", itinerary.getTitle());
        brief.put("city", itinerary.getCity());
        brief.put("travelDays", itinerary.getTravelDays());
        brief.put("aiSummary", itinerary.getAiSummary());
        brief.put("createdAt", itinerary.getCreatedAt() == null ? "" : itinerary.getCreatedAt().toString());
        return brief;
    }

    private JsonNode readPlan(String planJson) {
        try {
            return objectMapper.readTree(planJson);
        } catch (Exception e) {
            log.warn("plan_json 解析失败，按空对象返回 | {}", e.getMessage());
            return objectMapper.createObjectNode();
        }
    }
}
