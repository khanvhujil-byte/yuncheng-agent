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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..services.store import init_db
from .routes import trip

logger = logging.getLogger(__name__)

app = FastAPI(
    title="多智能体旅行规划助手",
    version="0.5.0",
    description="四个助手分工协作：景点搜集 / 天气查询 / 预订住宿 / 行程编排；"
    "含独立复核、行程库复用、后台任务与断点续跑",
)

# 先挂路由再建 app 的顺序坑（真踩过）：include_router 必须在 app = FastAPI() 之后，
# 否则启动就 NameError: name 'app' is not defined。
app.include_router(trip.router, prefix="/api")

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


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    """前端页面加载时会调它，用来提示「后端未连接」。"""
    return {
        "status": "ok",
        "service": "trip-planner-agent",
        "version": app.version,
    }
