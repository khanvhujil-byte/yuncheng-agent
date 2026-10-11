package com.trip.community.service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ZSetOperations;
import org.springframework.stereotype.Service;

import com.trip.community.entity.Itinerary;
import com.trip.community.mapper.ItineraryMapper;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * 排行榜。**热榜的关键不是排序，是时间衰减。**
 *
 * <p>如果只按点赞数排，一个半年前的老行程会永久霸榜，新发布的永远上不来。
 * 这里用 Hacker News 那套衰减：{@code hot = 点赞数 / (已过小时数 + 2)^1.5}，
 * 新内容有天然优势，老内容会自然下沉，但点赞特别多的仍能靠实力顶住。
 *
 * <p>为什么取 Top 200 再在 Java 里重排、而不是让 Redis 直接算衰减：
 * ZSET 的 score 在写入时就固定了，衰减是"随时间变化"的量 —— 要么定时重算所有 score（浪费），
 * 要么读的时候算（我们现在这样）。取 200 条是为了把重排成本限制在常数级。
 *
 * <p>这同时是"热度反哺 Python"的数据来源（见 {@code HotStatService}）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class LeaderboardService {

    private static final int SCAN_LIMIT = 200;
    private static final double DECAY_POWER = 1.5;
    private static final double OFFSET_HOURS = 2.0;

    private final StringRedisTemplate redis;
    private final ItineraryMapper itineraryMapper;
    private final LikeService likeService;

    /**
     * 热榜：Redis 出排序，MySQL 补详情。
     *
     * <p>为什么不把详情也塞 Redis：行程 JSON 体积大（一个完整 TripPlan 可能几十 KB），
     * 全塞进 Redis 既费内存又没必要 —— 排行榜只展示标题/城市/点赞数这些轻字段。
     */
    public List<Map<String, Object>> hotLeaderboard(int limit) {
        Set<ZSetOperations.TypedTuple<String>> tuples =
                redis.opsForZSet().reverseRangeWithScores(LikeService.LEADERBOARD_KEY, 0, SCAN_LIMIT - 1);
        if (tuples == null || tuples.isEmpty()) {
            return List.of();
        }

        long now = System.currentTimeMillis();
        List<Map<String, Object>> ranked = new ArrayList<>();
        for (ZSetOperations.TypedTuple<String> tuple : tuples) {
            if (tuple.getValue() == null) {
                continue;
            }
            long itineraryId = Long.parseLong(tuple.getValue());
            long likeCount = tuple.getScore() == null ? 0 : tuple.getScore().longValue();
            Itinerary itinerary = itineraryMapper.selectById(itineraryId);
            if (itinerary == null || itinerary.getStatus() == null || itinerary.getStatus() != 1) {
                // 已下架的行程顺手从榜上摘掉（读时清理，不用定时任务）
                redis.opsForZSet().remove(LikeService.LEADERBOARD_KEY, tuple.getValue());
                continue;
            }
            double hot = hotScore(likeCount, itinerary.getCreatedAt(), now);
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("itineraryId", itineraryId);
            item.put("title", itinerary.getTitle());
            item.put("city", itinerary.getCity());
            item.put("travelDays", itinerary.getTravelDays());
            item.put("aiSummary", itinerary.getAiSummary());
            item.put("likeCount", likeCount);
            item.put("hotScore", Math.round(hot * 100) / 100.0);
            ranked.add(item);
        }
        ranked.sort(Comparator.comparingDouble((Map<String, Object> m) -> (Double) m.get("hotScore")).reversed());
        return ranked.size() > limit ? new ArrayList<>(ranked.subList(0, limit)) : ranked;
    }

    /** 最新榜：按发布时间倒序，不参与热度竞争（给"刚发布"的内容曝光机会）。 */
    public List<Map<String, Object>> newestLeaderboard(int limit) {
        List<Itinerary> recent = itineraryMapper.selectList(
                new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<Itinerary>()
                        .eq("status", 1)
                        .orderByDesc("created_at")
                        .last("LIMIT " + Math.max(1, Math.min(limit, 50))));
        List<Map<String, Object>> result = new ArrayList<>();
        for (Itinerary itinerary : recent) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("itineraryId", itinerary.getId());
            item.put("title", itinerary.getTitle());
            item.put("city", itinerary.getCity());
            item.put("travelDays", itinerary.getTravelDays());
            item.put("aiSummary", itinerary.getAiSummary());
            item.put("likeCount", likeService.likeCount(itinerary.getId()));
            item.put("createdAt", itinerary.getCreatedAt() == null ? "" : itinerary.getCreatedAt().toString());
            result.add(item);
        }
        return result;
    }

    /** Hacker News 式时间衰减：hot = likes / (hours + 2)^1.5 */
    static double hotScore(long likeCount, LocalDateTime createdAt, long nowMillis) {
        double hours = OFFSET_HOURS;
        if (createdAt != null) {
            long created = createdAt.atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli();
            hours = Duration.ofMillis(Math.max(0, nowMillis - created)).toMinutes() / 60.0;
        }
        return likeCount / Math.pow(hours + OFFSET_HOURS, DECAY_POWER);
    }
}
