package com.trip.community.dto;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 发布行程的请求体 —— **这是跨语言的契约边界**。
 *
 * <p>字段名与 Python 侧 {@code TripRequest} 保持一致（snake_case 由 Jackson 自动映射），
 * {@code plan} 直接收 Python 的 {@code TripPlan} 原样 JSON，Java 不解析它的内部结构。
 */
public record PublishRequest(
        String title,
        String city,
        Integer travelDays,
        String startDate,
        /** Python 生成的完整 TripPlan（原样存库） */
        JsonNode plan) {
}
