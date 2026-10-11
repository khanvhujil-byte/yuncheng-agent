-- 业务库：行程社区（Java 侧持有连接，Python 不碰这个库）
-- 建库：mysql -uroot -proot < docs/schema/trip_community.sql
--
-- 设计要点：
--   1. tb_itinerary.plan_json 直接存 Python 那份 TripPlan 契约（字段名与 schemas.py 逐字对应），
--      两个服务之间不做字段翻译 —— 契约跨语言复用的成本最低。
--   2. tb_like_record 上的唯一键 uk_itinerary_user 是"一人一赞"的**数据库层兜底**：
--      并发场景下 Redis 先挡一层，落库时唯一键再挡一层（防御纵深）。
--   3. tb_hot_stat 是给 Python 反哺用的（"热度反哺生成"的落点），不放在 Redis 里是因为
--      它是"每天/每小时刷新的统计"，不是高频读写。

CREATE DATABASE IF NOT EXISTS trip_community
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE trip_community;

-- ---------------------------------------------------------------- 用户
CREATE TABLE IF NOT EXISTS tb_user (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  username      VARCHAR(64)  NOT NULL COMMENT '登录名',
  nickname      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '显示名',
  password_hash VARCHAR(100) NOT NULL DEFAULT '' COMMENT 'BCrypt 哈希，绝不存明文',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_username (username)
) ENGINE = InnoDB COMMENT '用户';

-- ---------------------------------------------------------------- 已发布行程
CREATE TABLE IF NOT EXISTS tb_itinerary (
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  user_id      BIGINT       NOT NULL COMMENT '发布者',
  title        VARCHAR(128) NOT NULL COMMENT '标题（用户填或 AI 生成）',
  city         VARCHAR(64)  NOT NULL,
  travel_days  INT          NOT NULL,
  start_date   DATE         NULL COMMENT '可能为空：发布的是"行程模板"而非某次具体出行',
  plan_json    JSON         NOT NULL COMMENT '完整 TripPlan（与 Python 契约同构）',
  ai_summary   VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'Python 回调生成的简介',
  like_count   INT          NOT NULL DEFAULT 0 COMMENT '点赞数（Redis 为准，这里最终一致）',
  view_count   INT          NOT NULL DEFAULT 0,
  status       TINYINT      NOT NULL DEFAULT 1 COMMENT '1=公开 0=下架',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_city_like (city, like_count DESC),
  KEY idx_user (user_id),
  KEY idx_created (created_at)
) ENGINE = InnoDB COMMENT '已发布的行程';

-- ---------------------------------------------------------------- 点赞流水
CREATE TABLE IF NOT EXISTS tb_like_record (
  id           BIGINT   NOT NULL AUTO_INCREMENT,
  itinerary_id BIGINT   NOT NULL,
  user_id      BIGINT   NOT NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_itinerary_user (itinerary_id, user_id)
) ENGINE = InnoDB COMMENT '点赞记录（一人一行，唯一键兜底）';

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
) ENGINE = InnoDB COMMENT '热度统计：Python 生成行程前读它做"反哺"';

-- 初始化几个热门项，让第一次生成就有东西可反哺（真实场景由定时任务刷）
INSERT IGNORE INTO tb_hot_stat (stat_type, stat_key, score) VALUES
  ('city', '杭州', 120), ('city', '成都', 90), ('city', '西安', 70),
  ('preference', '自然风光', 150), ('preference', '博物馆', 110), ('preference', '历史文化', 95),
  ('attraction', '西湖', 88), ('attraction', '灵隐寺', 61), ('attraction', '大熊猫繁育研究基地', 55);
