package com.trip.community.controller;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.trip.community.service.LeaderboardService;
import com.trip.community.service.LikeService;

import lombok.RequiredArgsConstructor;

/**
 * 点赞 + 排行榜。
 *
 * <p>点赞接口是这套系统里**唯一需要扛高并发**的写接口：
 * 它以 Redis 为准立即返回，MySQL 由后台每秒批量落库（见 {@link LikeService}）。
 * 返回值里带上最新的点赞数，让前端不用再发一次查询。
 */
@RestController
@RequiredArgsConstructor
public class LikeController {

    private final LikeService likeService;
    private final LeaderboardService leaderboardService;

    /** 点个赞。幂等：重复点返回 alreadyLiked 而不是报错。 */
    @PostMapping("/itineraries/{id}/like")
    public Map<String, Object> like(
            @PathVariable long id,
            @RequestHeader(value = "X-User-Id", defaultValue = "1") long userId) {

        int code = likeService.like(id, userId);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("itineraryId", id);
        result.put("likeCount", likeService.likeCount(id));
        switch (code) {
            case 1 -> {
                result.put("success", true);
                result.put("liked", true);
                result.put("message", "点赞成功");
            }
            case 0 -> {
                result.put("success", true);
                result.put("liked", false);
                result.put("message", "你已经点过赞了");
            }
            case -2 -> {
                result.put("success", false);
                result.put("liked", false);
                result.put("errorCode", "RATE_LIMITED");
                result.put("message", "点赞太频繁了，稍后再试");
            }
            default -> {
                result.put("success", false);
                result.put("liked", false);
                result.put("errorCode", "NOT_FOUND");
                result.put("message", "行程不存在或已下架");
            }
        }
        return result;
    }

    /** 点赞状态：数量 + 我点过没。 */
    @GetMapping("/itineraries/{id}/like")
    public Map<String, Object> status(
            @PathVariable long id,
            @RequestHeader(value = "X-User-Id", defaultValue = "1") long userId) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("itineraryId", id);
        result.put("likeCount", likeService.likeCount(id));
        result.put("hasLiked", likeService.hasLiked(id, userId));
        return result;
    }

    /**
     * 排行榜。
     *
     * @param type hot=按热度（带时间衰减） new=按发布时间
     */
    @GetMapping("/leaderboard")
    public Map<String, Object> leaderboard(
            @RequestParam(defaultValue = "hot") String type,
            @RequestParam(defaultValue = "20") int limit) {
        List<Map<String, Object>> items = "new".equalsIgnoreCase(type)
                ? leaderboardService.newestLeaderboard(limit)
                : leaderboardService.hotLeaderboard(limit);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("type", type);
        result.put("items", items);
        result.put("count", items.size());
        return result;
    }

    /** 运维指标：异步落库队列的积压与丢弃数（削峰到极限时看这里）。 */
    @GetMapping("/internal/like-stats")
    public Map<String, Object> likeStats() {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("pendingInQueue", likeService.pendingCount());
        result.put("dropped", likeService.droppedCount());
        return result;
    }
}
