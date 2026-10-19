package com.trip.community.service;

import java.net.http.HttpClient;
import java.time.Duration;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.server.ResponseStatusException;

/** Validate the actual Python session on every authenticated operation. */
@Service
public class SessionIdentity {
    private final RestClient client;

    public SessionIdentity(@Value("${ai.base-url:http://127.0.0.1:8000}") String baseUrl) {
        var factory = new JdkClientHttpRequestFactory(HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build());
        factory.setReadTimeout(Duration.ofSeconds(3));
        client = RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
    }

    public long resolve(String token, boolean required) {
        if (token == null || token.isBlank()) {
            if (!required) return 0;
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "请先登录");
        }
        if (token.length() > 256) throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        try {
            var result = client.get().uri("/api/auth/community").header("X-Session-Token", token).retrieve().body(Map.class);
            if (result == null || !(result.get("userId") instanceof Number id) || id.longValue() <= 0) {
                throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "身份服务响应无效");
            }
            return id.longValue();
        } catch (HttpClientErrorException e) {
            if (e.getStatusCode().value() == 429) throw new ResponseStatusException(HttpStatus.TOO_MANY_REQUESTS, "身份查询过于频繁，请稍后重试");
            if (e.getStatusCode().value() != 401 && e.getStatusCode().value() != 403) throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "身份服务响应异常");
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "登录已失效");
        } catch (ResponseStatusException e) {
            throw e;
        } catch (Exception e) {
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "身份服务暂时不可用");
        }
    }
}
