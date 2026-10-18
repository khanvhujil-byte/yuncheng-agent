"""配置加载与校验。

约定：只有本文件直接读环境变量，其他模块一律通过 get_settings() 取配置。
"""

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ------------------------------------------------------------------ 基础

# config.py 在 backend/app/ 下，往上一级就是 backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_DIR / ".env"

AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """配置错误——属于「不该启动」的级别，不是「降级继续」的级别。"""


# ------------------------------------------------------------------ 配置模型


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM（DeepSeek，走 OpenAI 兼容端点）----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model_id: str = "deepseek-chat"
    llm_timeout: float = 60.0

    # ---- 功能开关（对照实验用）----
    weather_use_agent: bool = True
    # ↑ 天气节点是否走 Agent（LLM 调工具）。
    #   True  = 天气也是 Agent（多智能体完整形态，多花 1~2 次 LLM 调用）
    #   False = 直接调工具（0 token，瞬时故障由运行时限次重试）
    #   这个开关本身就是个实验变量：量化「把确定性节点 Agent 化的代价」。
    #   改 .env 加一行 WEATHER_USE_AGENT=false 即可切换。

    # ---- 上下文压缩（对应 learn-claude-code 的 s08 Context Compact）----
    prompt_compact: bool = True
    # ↑ 是否对送进编排 prompt 的候选景点做**预算裁剪**。
    #   关闭它就回到 v0.3 的行为（全部候选都塞进 prompt），用于 A/B 实验量化省了多少 token。
    prompt_max_candidates: int = 24
    # ↑ 提示词里候选景点的上限。必去锚点永远在内，其余按高德评分排序取到上限。
    #   为什么是 24：编排每天用 2~3 个，天数上限 30 时 24 仍然够用（候选池本来就是超集）。

    # ---- 高德开放平台（Web 服务 Key，后端 REST 用）----
    amap_api_key: str = ""
    amap_timeout: float = 10.0
    amap_request_interval_seconds: float = Field(default=0.2, ge=0.01, le=10, allow_inf_nan=False)
    amap_rate_limit_cooldown_seconds: float = Field(default=1.0, ge=0.1, le=60, allow_inf_nan=False)
    map_backend: Literal["local", "mcp"] = "local"
    mcp_transport: Literal["stdio", "streamable_http"] = "stdio"
    mcp_command: str = ""
    mcp_args: list[str] = Field(default_factory=list)
    mcp_env: dict[str, str] = Field(default_factory=dict)
    mcp_url: str = ""
    mcp_headers: dict[str, str] = Field(default_factory=dict)
    mcp_timeout: float = Field(default=20.0, gt=0, le=60, allow_inf_nan=False)
    session_ttl_seconds: int = Field(default=604800, ge=60, le=2592000)
    trust_proxy_headers: bool = False

    # Read-only tools only; counts include the initial attempt.
    tool_max_attempts: int = Field(default=2, ge=1, le=3)
    tool_retry_budget_seconds: float = Field(default=20.0, gt=0, le=60, allow_inf_nan=False)
    tool_retry_base_delay: float = Field(default=0.25, ge=0, le=2, allow_inf_nan=False)

    # ---- 服务 ----
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"

    # ---- 与 Java 社区服务的联动（双向）----
    community_enabled: bool = True
    # ↑ 关掉它，Python 就完全不依赖 Java（生成照常，只是没有热度反哺、也发不了行程）。
    #   留这个开关的价值：**跨服务联调时能一键隔离**，排查"到底是哪一侧的问题"。
    community_base_url: str = "http://127.0.0.1:8080/community"
    community_timeout: float = 2.0
    # ↑ 读热度的超时（秒）。刻意给得很短：热度只是锦上添花，不值得为它等。
    #   发布行程那条路会单独放宽到 30 秒（因为 Java 要回调本服务生成摘要）。

    # ---- 上线必备：访问控制与限流（默认关，生产必须显式打开）----
    api_key: str = ""
    # ↑ 为空表示**不校验**（本地开发与 CI 友好）。生产设一个随机串：
    #   python -c "import secrets;print(secrets.token_urlsafe(32))"
    #   之后请求带 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`。
    rate_limit_per_minute: int = 0
    # ↑ 每客户端每分钟请求数上限。<=0 表示关闭（默认）。
    #   一次规划要花几十秒与一万多 token，生产建议 5~10 —— 限流保护的是上游配额和钱包。
    #   V0 是进程内滑动窗口；多副本要换 Redis 计数器。


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。改了 .env 需要重启进程（或 get_settings.cache_clear()）。"""
    return Settings()


def setup_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )

    # httpx 在 INFO 级别会把完整请求 URL 打进日志。而高德的 Key 只能拼在
    # query string 里传（?key=xxxx），等于每次调用都把密钥写进日志文件。
    # 压到 WARNING，正常请求就不再记录，出错时仍然能看到。
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ------------------------------------------------------------------ 工具


def mask_secret(secret: str) -> str:
    """日志脱敏：只留前 6 位。既不泄露完整 Key，又能让你确认加载的是哪一个。"""
    if not secret:
        return "(未配置)"
    if len(secret) <= 6:
        return "***"
    return secret[:6] + "..."


def probe_amap(api_key: str, timeout: float = 5.0) -> tuple[str, str]:
    """真实调用一次高德地理编码，判断 Key 到底能不能用。

    返回 (status, detail)，status 三种取值：
        "ok"           Key 可用
        "invalid"      Key 确定无效（高德明确回了错误码）
        "unreachable"  网络问题，无法判断 Key 是否可用

    区分 invalid 和 unreachable 是本函数存在的意义：
    前者该拦住不让启动，后者只是网络抖动，不该阻塞开发。
    """
    try:
        response = httpx.get(
            AMAP_GEOCODE_URL,
            params={"key": api_key, "address": "北京"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # httpx.HTTPError 覆盖超时 / 连接失败 / 4xx / 5xx
        # ValueError 覆盖响应体不是 JSON 的情况
        return "unreachable", f"{type(exc).__name__}: {exc}"

    infocode = str(payload.get("infocode", ""))
    info = str(payload.get("info", ""))
    if infocode == "10000":
        return "ok", "地理编码调用成功（infocode=10000）"
    return "invalid", f"高德返回 infocode={infocode}, info={info}"


# ------------------------------------------------------------------ 校验


def validate_config() -> Settings:
    settings = get_settings()

    # ---- 第 1 层：非空。缺了就拦住，不给启动 ----
    missing = [
        name
        for name, value in (
            ("LLM_API_KEY", settings.llm_api_key),
            ("AMAP_API_KEY", settings.amap_api_key if settings.map_backend == "local" else "remote"),
        )
        if not value.strip()
    ]
    if missing:
        raise ConfigError(
            f"缺少必需的配置项：{'、'.join(missing)}；请检查 {ENV_FILE}"
        )

    if settings.map_backend == "mcp":
        from urllib.parse import urlparse
        if settings.mcp_transport == "stdio" and not settings.mcp_command.strip():
            raise ConfigError("MCP stdio 模式必须设置 MCP_COMMAND")
        if settings.mcp_transport == "streamable_http":
            endpoint = urlparse(settings.mcp_url)
            if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
                raise ConfigError("MCP HTTP 模式必须设置合法的 MCP_URL")
        logger.info("地图采用 MCP %s，跳过本地高德 Key 探测。", settings.mcp_transport)
        return settings

    # ---- 第 2 层：格式。只警告，不拦截 ----
    if not re.fullmatch(r"[0-9a-fA-F]{32}", settings.amap_api_key):
        logger.warning(
            "AMAP_API_KEY 不是常见的 32 位十六进制格式（当前长度 %d）。"
            "高德若调整过 Key 格式可忽略本条，否则请确认没填成"
            " Web 端(JS API) 类型的 Key。",
            len(settings.amap_api_key),
        )
    if not settings.llm_api_key.startswith("sk-"):
        logger.warning("LLM_API_KEY 不是常见的 sk- 前缀，请确认没配错。")

    # ---- 第 3 层：真实探测 ----
    status, detail = probe_amap(settings.amap_api_key, timeout=5.0)
    if status == "invalid":
        raise ConfigError(f"高德 Key 校验失败：{detail}")
    if status == "unreachable":
        logger.warning(
            "无法连通高德接口（%s）。Key 是否可用未经验证，程序继续启动。", detail
        )
    else:
        logger.info("高德 Key 探测通过：%s", detail)

    # ---- 额外检查：系统环境里的 OPENAI_API_KEY ----
    if os.getenv("OPENAI_API_KEY"):
        logger.warning(
            "检测到系统环境变量 OPENAI_API_KEY 非空。本项目自己构造 ChatOpenAI，"
            "不受它影响；但若以后引入会做 provider 自动探测的框架（如 hello-agents），"
            "它可能劫持模型选择导致 401。本程序不会修改或删除该变量。"
        )

    logger.info(
        "配置校验通过 | model=%s | base_url=%s | amap_key=%s",
        settings.llm_model_id,
        settings.llm_base_url,
        mask_secret(settings.amap_api_key),
    )
    return settings


if __name__ == "__main__":
    setup_logging()
    validate_config()


