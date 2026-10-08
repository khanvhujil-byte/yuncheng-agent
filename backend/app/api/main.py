"""FastAPI 应用装配。

Day 1 待实现：
    - app = FastAPI(title=..., version=...)
    - CORS 中间件：允许 http://localhost:5173（前端端口）
      不放行的话浏览器直接拦掉请求，后端日志里连一条记录都看不到
    - 注册路由：app.include_router(trip.router, prefix="/api")
    - GET /health  →  {"status": "ok", "service": ..., "version": ...}
      前端页面加载时会调它，用来提示「后端未连接」

注意（需求文档「坑 3」）：
    本文件及全项目禁止 print emoji。Windows 中文控制台 GBK 编码不了
    U+1F680 之类的字符，会让 startup 事件抛 UnicodeEncodeError 直接退出，
    而且只在 stdout 被重定向成非 tty 时触发，非常难查。
    所有日志走 logging，不用 print。
"""
import app

"""FastAPI 应用装配。

本文件禁止 print emoji。Windows 中文控制台 GBK 编码不了 U+1F680 之类的字符，
会让 startup 事件抛 UnicodeEncodeError 导致应用直接退出。
日志一律走 logging。
"""

import logging
from .routes import trip
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

app = FastAPI(
    title="多智能体旅行规划助手",
    version="0.1.0",
    description="四个助手分工协作：景点搜集 / 天气查询 / 酒店推荐 / 行程编排",
)
app.include_router(trip.router, prefix="/api")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    """前端页面加载时会调它，用来提示「后端未连接」。"""
    return {
        "status": "ok",
        "service": "trip-planner-agent",
        "version": app.version,
    }
