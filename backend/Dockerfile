# 后端镜像。构建上下文是 backend/，所以 .env 不会被打进去（密钥永远走运行时注入）。
FROM node:22-bookworm-slim AS flyai-node
FROM python:3.13-slim

# Official FlyAI CLI runs as an isolated Node subprocess; no global npm install.
COPY --from=flyai-node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# 不写 .pyc、日志不缓冲（容器里日志必须能实时流出来，否则看不到进度）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷依赖清单再装：改了代码不会让依赖层缓存失效，重建从 1 分钟降到几秒
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
RUN python scripts/install_flyai.py

# 行程库（SQLite）落在 /app/data —— compose 里挂成 volume，容器删了数据还在
RUN mkdir -p /app/data

EXPOSE 8000

# 健康检查用 /health（前端页面加载时也调它）
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/health', timeout=2).raise_for_status()"

# 注意：这里不跑 run.py（它带"启动前校验 Key"的逻辑，需要真实 Key），
# 镜像本身要能在没有 Key 的情况下起来，好让人先看到 /docs 与清晰报错。
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
