package com.trip.community.controller;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.trip.community.service.HotStatService;

import lombok.RequiredArgsConstructor;

/**
 * 热度接口 —— **给 Python 反哺用**（这是闭环里 Java → Python 的那一段）。
 *
 * <p>Python 在生成行程前调它，拿到"社区现在热门什么"，把结果作为默认偏好或候选加权。
 * 于是「用户行为 → 热度统计 → AI 生成 → 新行程发布」形成闭环，
 * 而不是两个项目各干各的、只是互相调了一下接口。
 */
@RestController
@RequestMapping("/hot")
@RequiredArgsConstructor
public class HotController {

    private final HotStatService hotStatService;

    /**
     * @param type  city / preference / attraction；留空返回三类
     * @param limit 每类返回条数
     */
    @GetMapping
    public Map<String, Object> hot(
            @RequestParam(required = false) String type,
            @RequestParam(defaultValue = "5") int limit) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("stats", hotStatService.top(limit, type));
        result.put("source", "java-community/tb_hot_stat");
        return result;
    }

    /** 手动触发全量重算（累加式统计会漂移，重算是幂等的纠正手段）。 */
    @GetMapping("/recalc")
    public Map<String, Object> recalc() {
        hotStatService.recalcCityFromItineraries();
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("success", true);
        result.put("cities", hotStatService.top(10, "city").get("city"));
        return result;
    }

    /** 方便 Python 侧做连通性探测。 */
    @GetMapping("/ping")
    public List<String> ping() {
        return List.of("pong", "from-java-community");
    }
}
