package com.trip.community.controller;

import java.util.LinkedHashMap;
import java.util.Map;

import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * 健康检查。**它检查的是"依赖能不能用"，不只是"进程还在不在"**。
 *
 * <p>为什么要把 MySQL 与 Redis 的真实连通性暴露出来：
 * 这个服务对两者的依赖是硬依赖（点赞靠 Redis、行程落 MySQL），
 * 只返回 {@code {"status":"ok"}} 是自欺欺人 —— 负载均衡会把流量继续送进一个连不上库的实例。
 */
@Slf4j
@RestController
@RequestMapping("/health")
@RequiredArgsConstructor
public class HealthController {

    private final JdbcTemplate jdbcTemplate;
    private final StringRedisTemplate redisTemplate;

    @GetMapping
    public Map<String, Object> health() {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("service", "java-community");
        result.put("role", "业务与高并发（行程发布 / 点赞 / 排行榜）");

        result.put("mysql", checkMysql());
        result.put("redis", checkRedis());

        boolean healthy = "ok".equals(result.get("mysql")) && "ok".equals(result.get("redis"));
        result.put("status", healthy ? "ok" : "degraded");
        return result;
    }

    private String checkMysql() {
        try {
            Integer one = jdbcTemplate.queryForObject("SELECT 1", Integer.class);
            return Integer.valueOf(1).equals(one) ? "ok" : "unexpected";
        } catch (Exception e) {
            log.warn("MySQL 健康检查失败: {}", e.getMessage());
            return "down: " + e.getClass().getSimpleName();
        }
    }

    private String checkRedis() {
        try {
            String pong = redisTemplate.execute(connection -> connection.ping(), true);
            return "PONG".equalsIgnoreCase(pong) ? "ok" : String.valueOf(pong);
        } catch (Exception e) {
            log.warn("Redis 健康检查失败: {}", e.getMessage());
            return "down: " + e.getClass().getSimpleName();
        }
    }
}
