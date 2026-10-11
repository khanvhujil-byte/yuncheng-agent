package com.trip.community.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

import lombok.Data;

/** 热度统计项（city / preference / attraction）。 */
@Data
@TableName("tb_hot_stat")
public class HotStat {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String statType;
    private String statKey;
    private Integer score;
}
