"""行程规划路由。

router = APIRouter(prefix="/trip", tags=["旅行规划"])
在 main.py 里又叠了 "/api" 前缀，所以最终路径是 POST /api/trip/plan

Day 1 待实现（先用占位数据打通链路）：
    - 生成 thread_id = uuid4().hex
    - 构造初始 TripState（13 个字段必须全部给上，缺字段会在节点里抛 KeyError）
    - final_state = graph.invoke(initial_state)
    - 把 final_state 里的 plan / warnings / sources 包装成 TripPlanResponse

    注意：函数签名用 def 而不是 async def。
    graph.invoke() 是同步阻塞调用（真实版本要跑几十秒），写成 async def
    会堵住 FastAPI 的事件循环，其他请求全部排队。用 def 会自动进线程池。

Day 7 待实现：
    GET /api/trip/{thread_id}   读取历史状态（需要 Checkpointer）
"""
