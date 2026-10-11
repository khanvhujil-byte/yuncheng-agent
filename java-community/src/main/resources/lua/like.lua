-- 点赞的原子脚本：限频 → 一人一赞 → 计数 → 更新排行榜，四件事一次完成。
--
-- 为什么必须用 Lua 而不是在 Java 里写四步：
--   四步分开执行就会出现"并发窗口"：两个请求同时通过 SADD 检查，
--   计数被加两次而用户只点了一次（重复计数）；或者限频和点赞不在同一事务里被绕过。
--   Redis 单线程执行脚本 = 这四步之间不可能插进别的请求，这就是原子性。
--
-- KEYS[1] = like:users:{itineraryId}     去重集合（谁点过）
-- KEYS[2] = leaderboard:like             ZSET 排行榜（score = 点赞数）
-- KEYS[3] = like:count:{itineraryId}     计数器
-- KEYS[4] = rate:like:{userId}           限频计数器（分钟粒度）
-- ARGV[1] = userId
-- ARGV[2] = itineraryId
-- ARGV[3] = 每分钟最多几次（防刷阈值）
--
-- 返回：1 = 首次点赞成功；0 = 已经点过（幂等）；-2 = 触发限频

-- 1) 先限频（放在最前面：被限的请求一个字节都不会写进去）
local hits = redis.call('INCR', KEYS[4])
if hits == 1 then
    redis.call('EXPIRE', KEYS[4], 60)
end
if hits > tonumber(ARGV[3]) then
    return -2
end

-- 2) 一人一赞：SADD 返回 0 说明这个用户已经点过
if redis.call('SADD', KEYS[1], ARGV[1]) == 0 then
    return 0
end

-- 3) 计数 + 排行榜：ZADD 用点赞数当 score，热榜取 Top N 就是 ZREVRANGE
local count = redis.call('INCR', KEYS[3])
redis.call('ZADD', KEYS[2], count, ARGV[2])

-- 4) 顺手记录"谁在什么时候点的"，供异步落库查询（不落库也不影响正确性）
redis.call('LPUSH', 'like:pending', ARGV[2] .. ':' .. ARGV[1])
return 1
