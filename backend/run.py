"""后端启动入口。

用法：
    cd backend
    .\\.venv\\Scripts\\python.exe run.py

Day 1 待实现：
    - 调用 app.config.setup_logging() 配置日志
    - 调用 app.config.validate_config() 做三层校验，不通过就不启动
    - uvicorn.run("app.api.main:app", host=..., port=...)

注意：uvicorn.run 的第一个参数传「字符串」而不是 import 进来的 app 对象。
"""
"""后端启动入口。

用法：
    cd backend
    .\\.venv\\Scripts\\python.exe run.py
"""

import uvicorn

from app.config import setup_logging, validate_config


def main() -> None:
    setup_logging()

    # 校验不通过会抛 ConfigError，后面的服务就不会启动
    settings = validate_config()

    uvicorn.run(
        "app.api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
