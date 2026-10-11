package com.trip.community.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.ClassPathResource;
import org.springframework.data.redis.core.script.DefaultRedisScript;

/**
 * Redis 脚本装配。
 *
 * <p>把 Lua 脚本做成单例 Bean，而不是每次调用都读文件 + 编译：
 * 脚本编译有成本，而点赞是高频路径 —— 每次调用读一次磁盘是没必要的开销。
 */
@Configuration
public class RedisScriptConfig {

    @Bean
    public DefaultRedisScript<Long> likeScript() {
        DefaultRedisScript<Long> script = new DefaultRedisScript<>();
        script.setLocation(new ClassPathResource("lua/like.lua"));
        script.setResultType(Long.class);
        return script;
    }
}
