package com.trip.community.controller;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.trip.community.dto.PublishRequest;
import com.trip.community.service.ItineraryService;

import lombok.RequiredArgsConstructor;

/**
 * 行程的发布与查询 —— Python → Java 联动的落点。
 *
 * <p>发布接口被 Python 的生图链路调用（用户点"发布到社区"时）。
 * 用户身份先用 {@code X-User-Id} 头占位，接入 JWT 后换成从 token 解析。
 */
@RestController
@RequestMapping("/itineraries")
@RequiredArgsConstructor
public class ItineraryController {

    private final com.trip.community.service.SessionIdentity identity;

    private final ItineraryService itineraryService;

    @PostMapping
    public Map<String, Object> publish(
            @RequestBody PublishRequest request,
            @RequestHeader(value = "X-Session-Token", required = false) String token) {
        long userId = identity.resolve(token, true);

        long id = itineraryService.publish(
                userId, request.title(), request.city(),
                request.travelDays(), request.startDate(), request.plan(), request.preferences());

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("success", true);
        result.put("itineraryId", id);
        result.put("url", "/community/itineraries/" + id);
        return result;
    }

    @GetMapping("/{id}")
    public ResponseEntity<Map<String, Object>> detail(
            @PathVariable long id,
            @RequestHeader(value = "X-Session-Token", required = false) String token) {
        long userId = identity.resolve(token, false);
        Map<String, Object> detail = itineraryService.detail(id, userId);
        return detail == null ? ResponseEntity.notFound().build() : ResponseEntity.ok(detail);
    }

    @GetMapping
    public List<Map<String, Object>> list(
            @RequestParam(required = false) String city,
            @RequestParam(defaultValue = "20") int limit) {
        return itineraryService.listByCity(city, limit);
    }
}
