package com.trip.community.service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import com.trip.community.entity.LikeRecord;
import com.trip.community.mapper.ItineraryMapper;
import com.trip.community.mapper.LikeRecordMapper;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * 点赞：高并发的核心。
 *
 * <p>整条链路的设计取舍（每一句都能在面试里展开）：
 *
 * <p><b>1. 写路径只碰 Redis。</b> Lua 一次原子完成"限频 + 一人一赞 + 计数 + 排行榜"，
 * 接口立刻返回。如果每个点赞都同步写 MySQL，QPS 就被数据库的写能力钉死在几百，
 * 而且高峰期会把连接池占满、连累读接口。
 *
 * <p><b>2. MySQL 异步批量落库，且是"从流水重算"而不是"累加"。</b>
 * 落库慢一点没关系（最终一致），但**绝不能算错**。重算幂等，重复执行无害。
 *
 * <p><b>3. 读路径防穿透。</b>被点/被看的行程可能已被删除，但请求仍会打进来；
 * 缓存一个空标记，省掉对数据库的无效查询（这就是缓存三件套里的"防穿透"）。
 *
 * <p><b>4. 限频在 Redis 里做而不是在应用内存里做。</b>多副本部署时内存计数各算各的，
 * 等于没限流。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class LikeService {

    /** 单用户每分钟最多点赞次数（防刷）。真实场景可按用户等级调整。 */
    private static final int MAX_LIKES_PER_MINUTE = 10;

    /** 空值标记的 TTL：防止"不存在的行程"被无限缓存，也防止存在后一直读不到。 */
    private static final Duration EMPTY_MARKER_TTL = Duration.ofMinutes(2);

    /** 每个行程的点赞用户集合、计数器、排行榜 key。 */
    private static final String USER_SET_KEY = "like:users:%d";
    private static final String COUNT_KEY = "like:count:%d";
    private static final String RATE_KEY = "rate:like:%d";
    private static final String META_KEY = "itinerary:meta:%d";
    public static final String LEADERBOARD_KEY = "leaderboard:like";

    private final StringRedisTemplate redis;
    private final DefaultRedisScript<Long> likeScript;
    private final ItineraryMapper itineraryMapper;
    private final LikeRecordMapper likeRecordMapper;

    // Durable outbox written inside the same Lua execution as the like.
    private static final String PENDING_KEY = "like:pending";

    /**
     * 点个赞。
     *
     * @return 1=成功 0=已点过 -1=行程不存在 -2=太快了
     */
    public int like(long itineraryId, long userId) {
        if (!exists(itineraryId)) {
            return -1;
        }
        Long result = redis.execute(
                likeScript,
                List.of(
                        USER_SET_KEY.formatted(itineraryId),
                        LEADERBOARD_KEY,
                        COUNT_KEY.formatted(itineraryId),
                        RATE_KEY.formatted(userId)),
                String.valueOf(userId),
                String.valueOf(itineraryId),
                String.valueOf(MAX_LIKES_PER_MINUTE));
        int code = result == null ? -1 : result.intValue();
        return code;
    }

    /** 当前点赞数：Redis 为准（未落库的也算），查不到再从库兜底。 */
    public long likeCount(long itineraryId) {
        String value = redis.opsForValue().get(COUNT_KEY.formatted(itineraryId));
        if (value != null) {
            return Long.parseLong(value);
        }
        Integer db = itineraryMapper.selectLikeCount(itineraryId);
        if (db != null) {
            // 回填 Redis，下次就不用查库了
            redis.opsForValue().set(COUNT_KEY.formatted(itineraryId), String.valueOf(db));
            return db;
        }
        return 0;
    }

    public boolean hasLiked(long itineraryId, long userId) {
        return Boolean.TRUE.equals(
                redis.opsForSet().isMember(USER_SET_KEY.formatted(itineraryId), String.valueOf(userId)));
    }

    /**
     * 行程是否存在 —— 带空值缓存的读路径（防穿透）。
     *
     * <p>顺序很重要：先查 Redis（快），miss 才查 MySQL，查不到就写一个短 TTL 的空标记。
     * 这样恶意刷"不存在的 id"只会打到 Redis。
     */
    private boolean exists(long itineraryId) {
        String metaKey = META_KEY.formatted(itineraryId);
        Boolean cached = redis.hasKey(metaKey);
        if (Boolean.TRUE.equals(cached)) {
            // meta 的 city 字段为空串表示"确认不存在"（空值标记）
            Object city = redis.opsForHash().get(metaKey, "city");
            return city != null && !city.toString().isEmpty();
        }
        var itinerary = itineraryMapper.selectById(itineraryId);
        if (itinerary == null || itinerary.getStatus() == null || itinerary.getStatus() != 1) {
            redis.opsForHash().put(metaKey, "city", "");
            redis.expire(metaKey, EMPTY_MARKER_TTL);
            return false;
        }
        redis.opsForHash().put(metaKey, "city", itinerary.getCity());
        redis.opsForHash().put(metaKey, "createdAt",
                itinerary.getCreatedAt() == null ? "" : itinerary.getCreatedAt().toString());
        redis.expire(metaKey, Duration.ofHours(2));
        return true;
    }

    /**
     * 让一条新行程**进入榜单**（score = 0）。
     *
     * <p>为什么发布时就要写 ZSET：热榜是从 Redis ZSET 读的，而 ZSET 原先只在"被点赞"时写入，
     * 结果就是**刚发布的行程在热榜里根本不存在** —— 用户发布后点"去社区榜看看"
     * 落在默认的热门榜上，看不到自己刚发的东西，会以为发布失败。
     * （真机验证时被抓到的产品问题。）
     *
     * <p>给它 0 分而不是"直接排前面"：热度就是热度，新发布且没人赞就该在榜尾，
     * 时间衰减会随着时间推移自然把它往上带。这里只解决"可见性"，不伪造热度。
     */
    public void enterLeaderboard(long itineraryId) {
        redis.opsForZSet().add(LEADERBOARD_KEY, String.valueOf(itineraryId), 0);
        log.debug("行程进入榜单（0 赞） | id={}", itineraryId);
    }

    /** 把 Redis 里的计数回填进 meta，供排行榜读取时做时间衰减。 */
    public void warmMeta(long itineraryId, String city, String createdAt) {
        String metaKey = META_KEY.formatted(itineraryId);
        redis.opsForHash().put(metaKey, "city", city);
        redis.opsForHash().put(metaKey, "createdAt", createdAt);
        redis.expire(metaKey, Duration.ofHours(2));
    }

    /**
     * 批量落库。每秒一次，一次最多 500 条。
     *
     * <p>为什么要"攒一批再写"：单条 INSERT 的往返开销远大于写入本身，
     * 500 条一起写（配合 rewriteBatchedStatements）能把数据库压力降一个量级。
     * 这也是"异步落库"能扛住高并发的关键，而不只是"丢到后台线程"那么简单。
     */
    @Scheduled(fixedDelay = 1000)
    public void flushToDatabase() {
        // Read without removing. A crash before ACK replays this exact batch.
        List<String> entries = redis.opsForList().range(PENDING_KEY, -500, -1);
        if (entries == null || entries.isEmpty()) return;
        List<LikeRecord> batch = new ArrayList<>();
        for (String entry : entries) {
            String[] parts = entry.split(":", -1);
            if (parts.length != 2) {
                log.error("Invalid durable like event; retained for operator repair");
                return;
            }
            try {
                batch.add(new LikeRecord(null, Long.parseLong(parts[0]), Long.parseLong(parts[1]), null));
            } catch (NumberFormatException e) {
                log.error("Invalid durable like identifiers; retained for operator repair");
                return;
            }
        }
        try {
            likeRecordMapper.batchInsertIgnore(batch);
            batch.stream().map(LikeRecord::getItineraryId).distinct().forEach(itineraryMapper::recalcLikeCount);
            // ACK only after both durable records and derived counts succeed.
            // INSERT IGNORE + unique(itinerary_id,user_id) makes replay safe.
            for (String entry : entries) redis.opsForList().remove(PENDING_KEY, 1, entry);
        } catch (DataAccessException e) {
            log.error("Like persistence failed; durable outbox retained for retry", e);
        }
    }

    public long droppedCount() {
        return 0; // Events are never silently discarded.
    }

    public int pendingCount() {
        Long size = redis.opsForList().size(PENDING_KEY);
        return size == null ? 0 : (int) Math.min(Integer.MAX_VALUE, size);
    }
}
