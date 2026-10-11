package com.trip.community.dto;

import java.util.List;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 发布行程的请求体 —— **这是跨语言的契约边界**。
 *
 * <p>字段名与 Python 侧保持一致（Jackson 自动映射），
 * {@code plan} 直接收 Python 的 {@code TripPlan} 原样 JSON，Java 不解析它的内部结构。
 *
 * <p>为什么 {@code preferences} 要单独传，而不是从 plan 里抠：
 * 行程（TripPlan）里只有"排出来的一天一天"，**不含用户当初勾选的偏好标签** ——
 * 那是请求（TripRequest）的信息。而热度统计要的正是偏好，所以它必须显式传过来。
 */
public record PublishRequest(
        String title,
        String city,
        Integer travelDays,
        String startDate,
        /** 用户勾选的偏好标签（如 自然风光 / 博物馆）。用于累积"常见偏好"热度。 */
        List<String> preferences,
        /** Python 生成的完整 TripPlan（原样存库） */
        JsonNode plan) {
}
