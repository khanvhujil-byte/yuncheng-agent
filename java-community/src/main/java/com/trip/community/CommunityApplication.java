package com.trip.community;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 行程社区服务（Java 侧）。
 *
 * <p>职责边界（与 Python AI 服务分工）：
 * <ul>
 *   <li><b>本服务</b>：用户 / 行程发布 / 点赞 / 排行榜 / 业务库（MySQL）连接 —— 也就是"钱和用户"</li>
 *   <li><b>Python 服务</b>：多智能体生成 / SSE / 上下文压缩 / 独立复核 —— 也就是"算力与缓存"</li>
 * </ul>
 *
 * <p>{@code @EnableAsync} 是为了点赞的<b>异步落库</b>：
 * 点赞以 Redis 为准立即返回，MySQL 由后台线程批量写入（最终一致）。
 * 不这么做的话，高并发下每个点赞都要等一次数据库往返，QPS 直接被 DB 掐死。
 *
 * <p>{@code @EnableScheduling} 用于把热度统计定时刷进 MySQL（给 Python 反哺用）。
 */
@SpringBootApplication
@MapperScan("com.trip.community.mapper")
@EnableAsync
@EnableScheduling
public class CommunityApplication {

    public static void main(String[] args) {
        SpringApplication.run(CommunityApplication.class, args);
    }
}
