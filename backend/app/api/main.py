"""FastAPI 应用装配。

职责只有四件：建 app、放行 CORS、挂路由、开数据库。
业务逻辑一行都不在这里。

CORS 必须放行前端端口，否则浏览器直接拦掉请求 —— 而且**后端日志里连一条记录都没有**，
排查时容易误判成"后端没起"。

本文件及全项目禁止 print emoji。Windows 中文控制台 GBK 编码不了 U+1F680 之类的字符，
会让 startup 事件抛 UnicodeEncodeError 直接退出，而且只在 stdout 被重定向成非 tty 时触发。
日志一律走 logging。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings
from ..services import store
from ..services.store import init_db
from .routes import ai, community, trip, dialog, travel

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app):
    # 上一次进程崩溃/重启留下的 running 任务不可能再完成；不回收会让结果
    # 端点永远返回"仍在生成中"、SSE 永不结束。
    store.mark_stale_jobs()
    from ..services.catalog_refresh import RefreshWorker
    worker = RefreshWorker()
    worker.start()
    try:
        yield
    finally:
        worker.stop()
        from ..services.mcp_pool import close
        close()


app = FastAPI(
    lifespan=lifespan,
    title="多智能体旅行规划助手",
    version="0.8.0",
    description="四个助手分工协作：景点搜集 / 天气查询 / 预订住宿 / 行程编排；"
    "含独立复核、行程库复用、后台任务与断点续跑",
)

# 先挂路由再建 app 的顺序坑（真踩过）：include_router 必须在 app = FastAPI() 之后，
# 否则启动就 NameError: name 'app' is not defined。
app.include_router(trip.router, prefix="/api")
# AI 能力接口：给 Java 社区服务调用（Java → Python 方向的联动）。
# 它**不加鉴权与限流**：只在内网被 Java 调用，对外不暴露（生产用 nginx 挡 /api/ai）。
app.include_router(ai.router, prefix="/api")
# 社区联动接口：前端从这里发布行程 / 看热度，Python 再把请求转给 Java。
app.include_router(community.router, prefix="/api")
app.include_router(dialog.router, prefix="/api")
app.include_router(travel.router, prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # 5174 是「不看后端端口时起第二份前端」用的备用端口，本地开发一并放行
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 建表（幂等）。放在模块级而不是 startup 事件里：SQLite 建表是同步且极快的，
# 这样第一个请求进来时库一定已经就绪，少一个"启动钩子没跑到"的失败模式。
init_db()
from ..services.dialog_store import recover_interrupted  # noqa: E402
recover_interrupted()

# 启动时把"生产该开而没开"的开关喊出来。默认关是为了本地开发和 CI 方便，
# 但绝不能默默放行 —— 这类"忘了开鉴权"的问题上线后没人会主动发现。
_settings = get_settings()
if _settings.deployment_mode == 'production':
    if not _settings.llm_api_key or (_settings.map_backend == 'local' and not _settings.amap_api_key):
        raise RuntimeError('生产模式必须配置模型与地图凭据。')
    if _settings.rate_limit_per_minute <= 0:
        raise RuntimeError('生产模式必须开启请求限流。')
if not (_settings.api_key or "").strip():
    logger.warning("API_KEY 未设置：鉴权已关闭，仅适合本地开发；生产请在 .env 里设置。")
if int(_settings.rate_limit_per_minute or 0) <= 0:
    logger.warning("RATE_LIMIT_PER_MINUTE 未设置：限流已关闭；生产建议 5~10。")


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    """前端页面加载时会调它，用来提示「后端未连接」。"""
    return {
        "status": "ok",
        "service": "trip-planner-agent",
        "version": app.version,
    }


@app.get("/metrics", tags=["系统"], summary="监控指标（Prometheus 文本格式）")
def metrics() -> Response:
    """手工拼 Prometheus 文本格式，**零新依赖**。

    为什么必须有：没有指标就只能靠用户投诉发现问题；有了它，告警规则可以直接写
    "cache_hit_rate 连续 10 分钟为 0" 或 "avg_ms_generated > 120000"。

    为什么不做额外埋点：这些数字**全部来自 run_log 表** —— 每次请求都往里记了一行。
    能算出来的指标就别再加埋点，否则两套数据迟早对不上。
    """
    stats = store.stats()
    lines = [
        "# HELP trip_runs_total 累计请求数",
        "# TYPE trip_runs_total counter",
        f"trip_runs_total {stats['total_runs']}",
        "# HELP trip_cached_runs_total 走行程库缓存的请求数",
        "# TYPE trip_cached_runs_total counter",
        f"trip_cached_runs_total {stats['cached_runs']}",
        "# HELP trip_cache_hit_ratio 缓存命中率",
        "# TYPE trip_cache_hit_ratio gauge",
        f"trip_cache_hit_ratio {stats['cache_hit_rate']}",
        "# HELP trip_itineraries_stored 行程库里的行程条数",
        "# TYPE trip_itineraries_stored gauge",
        f"trip_itineraries_stored {stats['itineraries_stored']}",
        "# HELP trip_llm_calls_total 累计 LLM 调用次数（只统计真实生成的请求）",
        "# TYPE trip_llm_calls_total counter",
        f"trip_llm_calls_total {stats['llm_calls_generated']}",
        "# HELP trip_tokens_total 累计 token（只统计真实生成的请求）",
        "# TYPE trip_tokens_total counter",
        f"trip_tokens_total {stats['tokens_generated']}",
    ]
    if stats["avg_ms_generated"] is not None:
        lines += [
            "# HELP trip_avg_generate_ms 未命中缓存的平均耗时（毫秒）",
            "# TYPE trip_avg_generate_ms gauge",
            f"trip_avg_generate_ms {stats['avg_ms_generated']:.0f}",
        ]
    if stats["avg_ms_cached"] is not None:
        lines += [
            "# HELP trip_avg_cached_ms 命中缓存的平均耗时（毫秒）",
            "# TYPE trip_avg_cached_ms gauge",
            f"trip_avg_cached_ms {stats['avg_ms_cached']:.0f}",
        ]
    from ..services.monitoring import summary
    quality = summary()
    for key in ("sample_runs", "sample_tool_calls", "first_success_rate", "retry_rate", "final_success_rate", "tool_attempts", "empty_search_calls", "logical_tool_calls", "local_retrieval_hits", "weather_cache_hits"):
        lines.extend([f"# TYPE trip_quality_{key} gauge", f"trip_quality_{key} {quality[key]}"])
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")
