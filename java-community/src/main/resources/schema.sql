-- 业务库表结构。由 Spring Boot 启动时自动执行（spring.sql.init.mode=always）。
-- 幂等：全是 IF NOT EXISTS，重复启动不会报错，也不会丢数据。
--
-- 为什么放在 Java 侧而不是给 Python 的 docs/schema：
--   这个库**只由 Java 持有连接**。DDL 跟着持有者走，否则两边都会以为对方负责建表。
--   （docs/schema/trip_community.sql 是同一份 DDL 的文档版本，给人看的。）

-- ---------------------------------------------------------------- 用户
CREATE TABLE IF NOT EXISTS tb_user (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  username      VARCHAR(64)  NOT NULL COMMENT '登录名',
  nickname      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '显示名',
  password_hash VARCHAR(100) NOT NULL DEFAULT '' COMMENT 'BCrypt 哈希，绝不存明文',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_username (username)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '用户';

-- ---------------------------------------------------------------- 已发布行程
CREATE TABLE IF NOT EXISTS tb_itinerary (
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  user_id      BIGINT       NOT NULL COMMENT '发布者',
  title        VARCHAR(128) NOT NULL COMMENT '标题',
  city         VARCHAR(64)  NOT NULL,
  travel_days  INT          NOT NULL,
  start_date   DATE         NULL COMMENT '可空：发布的可能是"行程模板"而不是某次具体出行',
  plan_json    JSON         NOT NULL COMMENT '完整 TripPlan（与 Python 契约同构，不做字段翻译）',
  ai_summary   VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'Python 回调生成的简介',
  like_count   INT          NOT NULL DEFAULT 0 COMMENT '点赞数（Redis 为准，此处最终一致）',
  view_count   INT          NOT NULL DEFAULT 0,
  status       TINYINT      NOT NULL DEFAULT 1 COMMENT '1=公开 0=下架',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_city_like (city, like_count DESC),
  KEY idx_user (user_id),
  KEY idx_created (created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '已发布行程';

-- ---------------------------------------------------------------- 点赞流水
-- ★ uk_itinerary_user 是"一人一赞"的**数据库层兜底**：
--   Redis 先挡一层（快），落库时唯一键再挡一层（准）。并发下这两层缺一不可。
CREATE TABLE IF NOT EXISTS tb_like_record (
  id           BIGINT   NOT NULL AUTO_INCREMENT,
  itinerary_id BIGINT   NOT NULL,
  user_id      BIGINT   NOT NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_itinerary_user (itinerary_id, user_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '点赞记录';

-- ---------------------------------------------------------------- 热度统计（反哺 Python）
CREATE TABLE IF NOT EXISTS tb_hot_stat (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  stat_type  VARCHAR(32)  NOT NULL COMMENT 'city / preference / attraction',
  stat_key   VARCHAR(128) NOT NULL,
  score      INT          NOT NULL DEFAULT 0,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_type_key (stat_type, stat_key),
  KEY idx_type_score (stat_type, score DESC)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '热度统计：Python 生成前读它做反哺';
