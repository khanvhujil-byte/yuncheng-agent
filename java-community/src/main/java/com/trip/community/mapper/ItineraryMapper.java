package com.trip.community.mapper;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.trip.community.entity.Itinerary;

@Mapper
public interface ItineraryMapper extends BaseMapper<Itinerary> {

    /**
     * 用点赞流水**重算** like_count。
     *
     * <p>为什么不写 {@code SET like_count = like_count + 1}：
     * 应用层累加在并发/重放下会加错（同一批记录被处理两次就多加）。而从流水重算是**幂等**的 ——
     * 跑一次和跑十次结果一样。异步落库最怕的就是"重复执行会不会出错"，这样写就彻底不怕。
     */
    @Update("""
            UPDATE tb_itinerary i
            SET i.like_count = (SELECT COUNT(*) FROM tb_like_record r WHERE r.itinerary_id = i.id)
            WHERE i.id = #{itineraryId}
            """)
    int recalcLikeCount(@Param("itineraryId") Long itineraryId);

    @Select("SELECT like_count FROM tb_itinerary WHERE id = #{itineraryId}")
    Integer selectLikeCount(@Param("itineraryId") Long itineraryId);
}
