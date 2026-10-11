package com.trip.community.mapper;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.trip.community.entity.HotStat;

@Mapper
public interface HotStatMapper extends BaseMapper<HotStat> {

    /**
     * 累加式 upsert：靠唯一键 (stat_type, stat_key) 实现"有则加、无则插"。
     *
     * <p>用一条 SQL 而不是"先查再写"：后者在并发下会两个线程都判定"不存在"然后都插入，
     * 唯一键会报错（或者统计少了）。让数据库用一条原子语句处理，是这类计数的标准做法。
     */
    @Insert("""
            INSERT INTO tb_hot_stat (stat_type, stat_key, score) VALUES (#{type}, #{key}, #{delta})
            ON DUPLICATE KEY UPDATE score = score + VALUES(score)
            """)
    int upsertScore(@Param("type") String type, @Param("key") String key, @Param("delta") int delta);
}
