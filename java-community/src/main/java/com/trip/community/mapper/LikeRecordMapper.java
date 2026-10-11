package com.trip.community.mapper;

import java.util.List;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.trip.community.entity.LikeRecord;

@Mapper
public interface LikeRecordMapper extends BaseMapper<LikeRecord> {

    /**
     * 批量插入点赞记录，重复的直接忽略。
     *
     * <p>两个关键点：
     * <ol>
     *   <li>{@code INSERT IGNORE} + 唯一键 → 并发/重放下不会报错，也不会写重</li>
     *   <li>批量（配合 JDBC 的 {@code rewriteBatchedStatements=true}）→ 一次网络往返写多条，
     *       这是"异步落库"真正省时间的地方；单条一条条写反而比同步还慢</li>
     * </ol>
     */
    @Insert("""
            <script>
            INSERT IGNORE INTO tb_like_record (itinerary_id, user_id) VALUES
            <foreach collection="records" item="r" separator=",">
                (#{r.itineraryId}, #{r.userId})
            </foreach>
            </script>
            """)
    int batchInsertIgnore(@Param("records") List<LikeRecord> records);
}
