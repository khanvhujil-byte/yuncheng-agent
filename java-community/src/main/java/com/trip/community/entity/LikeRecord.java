package com.trip.community.entity;

import java.time.LocalDateTime;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 点赞记录（异步落库的最终一致）。
 *
 * <p>表上有唯一键 {@code uk_itinerary_user}：即使 Redis 那层被绕过（比如缓存被清空后重放），
 * 数据库这一层仍能挡住重复插入 —— 这叫防御纵深，不是重复劳动。
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
@TableName("tb_like_record")
public class LikeRecord {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long itineraryId;
    private Long userId;
    private LocalDateTime createdAt;
}
