package com.trip.community.service;

import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.data.redis.core.ListOperations;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import com.trip.community.mapper.ItineraryMapper;
import com.trip.community.mapper.LikeRecordMapper;

class LikeServiceTest {
    StringRedisTemplate redis;
    ListOperations<String,String> queue;
    ItineraryMapper itineraries;
    LikeRecordMapper likes;
    LikeService service;

    @BeforeEach @SuppressWarnings("unchecked")
    void setup() {
        redis=mock(StringRedisTemplate.class);
        queue=mock(ListOperations.class);
        when(redis.opsForList()).thenReturn(queue);
        itineraries=mock(ItineraryMapper.class);
        likes=mock(LikeRecordMapper.class);
        service=new LikeService(redis,new DefaultRedisScript<Long>(),itineraries,likes);
        when(queue.range("like:pending",-500,-1)).thenReturn(List.of("7:9"));
    }

    @Test void ackOccursOnlyAfterRecordsAndCountsPersist() {
        service.flushToDatabase();
        var order=inOrder(likes,itineraries,queue);
        order.verify(likes).batchInsertIgnore(anyList());
        order.verify(itineraries).recalcLikeCount(7L);
        order.verify(queue).remove("like:pending",1,"7:9");
    }

    @Test void databaseFailureRetainsDurableEvent() {
        doThrow(new DataAccessResourceFailureException("offline")).when(likes).batchInsertIgnore(anyList());
        service.flushToDatabase();
        verify(queue,never()).remove(anyString(),anyLong(),any());
    }

    @Test void countFailureAndRestartReplayRemainIdempotent() {
        doThrow(new DataAccessResourceFailureException("offline")).doReturn(1).when(itineraries).recalcLikeCount(7L);
        service.flushToDatabase();
        verify(queue,never()).remove(anyString(),anyLong(),any());
        new LikeService(redis,new DefaultRedisScript<Long>(),itineraries,likes).flushToDatabase();
        verify(likes,times(2)).batchInsertIgnore(anyList());
        verify(queue).remove("like:pending",1,"7:9");
    }

    @Test void malformedEventIsRetainedAndNotCountedAsDropped() {
        when(queue.range("like:pending",-500,-1)).thenReturn(List.of("invalid"));
        service.flushToDatabase();
        verifyNoInteractions(likes);
        verify(queue,never()).remove(anyString(),anyLong(),any());
        assertEquals(0,service.droppedCount());
    }
}
