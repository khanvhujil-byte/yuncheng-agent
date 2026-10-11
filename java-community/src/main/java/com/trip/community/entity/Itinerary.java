package com.trip.community.entity;

import java.time.LocalDate;
import java.time.LocalDateTime;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

import lombok.Data;

/**
 * 已发布的行程。
 *
 * <p>{@code planJson} 里存的是 Python 那份 {@code TripPlan} 契约（字段名 snake_case 一一对应）。
 * 两个服务之间**不做字段翻译** —— 翻译层是 bug 的温床，而这里的字段本来就是同一套语义。
 */
@Data
@TableName("tb_itinerary")
public class Itinerary {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;
    private String title;
    private String city;
    private Integer travelDays;
    private LocalDate startDate;

    /** 完整 TripPlan（JSON 字符串）。用 String 而不是对象：Java 侧不解析它，只负责存取。 */
    private String planJson;

    /** Python 回调生成的简介（Java → Python 方向的联动落点）。 */
    private String aiSummary;

    private Integer likeCount;
    private Integer viewCount;
    private Integer status;
    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;
}
