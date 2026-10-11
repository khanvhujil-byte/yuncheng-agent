package com.trip.community.service;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.reactive.function.client.WebClient;

import lombok.extern.slf4j.Slf4j;

/**
 * 与 Python AI 服务的联动（Java → Python 方向）。
 *
 * <p>目前两个用途：
 * <ol>
 *   <li><b>生成摘要</b>：行程发布时让 Python 写一句简介（它手里有模型，Java 不该自己造）</li>
 *   <li><b>读热度</b>：把热门城市/偏好喂给生成链路（在 Python 侧调用，见 {@link HotStatService}）</li>
 * </ol>
 *
 * <p><b>失败必须降级，不能拖垮主流程</b>：Python 挂了不该导致"发布行程"失败。
 * 所以这里所有调用都有超时 + 兜底返回值，并在日志里留痕 —— 这叫"依赖降级"，
 * 也是 Java 作为网关该做的事（12-Factor 里"外部依赖都要有失败路径"）。
 */
@Slf4j
@Service
public class AiClientService {

    private final WebClient webClient;
    private final boolean enabled;
    private final long summaryTimeoutMs;

    public AiClientService(
            @Value("${ai.base-url:http://127.0.0.1:8000}") String baseUrl,
            @Value("${ai.enabled:true}") boolean enabled,
            @Value("${ai.summary-timeout-ms:20000}") long summaryTimeoutMs) {
        this.webClient = WebClient.builder().baseUrl(baseUrl).build();
        this.enabled = enabled;
        this.summaryTimeoutMs = summaryTimeoutMs;
    }

    /**
     * 让 Python 生成一句行程简介。
     *
     * @return 摘要文本；Python 不可用或关闭时返回空串（**不是抛异常**）
     */
    @SuppressWarnings("unchecked")
    public String generateSummary(String city, int travelDays, String planJson) {
        if (!enabled || planJson == null || planJson.isBlank()) {
            return "";
        }
        try {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("city", city);
            body.put("travel_days", travelDays);
            body.put("plan_json", planJson);

            Map<String, Object> response = webClient.post()
                    .uri("/api/ai/summary")
                    .contentType(MediaType.APPLICATION_JSON)
                    .bodyValue(body)
                    .retrieve()
                    .bodyToMono(Map.class)
                    .block(java.time.Duration.ofMillis(summaryTimeoutMs));
            if (response == null) {
                return "";
            }
            Object summary = response.get("summary");
            return summary == null ? "" : summary.toString();
        } catch (Exception e) {
            log.warn("调用 Python 生成摘要失败，发布流程继续（摘要留空）| {}", e.getMessage());
            return "";
        }
    }

    /** 给 Python 的健康探测用：Java 会告诉它"这边能连上吗"。 */
    public boolean isAiReachable() {
        if (!enabled) {
            return false;
        }
        try {
            webClient.get().uri("/health").retrieve().bodyToMono(String.class)
                    .block(java.time.Duration.ofSeconds(2));
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** 组装 Python 侧的请求体（供 HotStatService 使用）。 */
    public List<String> noop() {
        return List.of();
    }
}
