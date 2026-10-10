"""配置加载与校验。

Day 1 待实现：
    - 用 pydantic-settings 定义 Settings，读取 backend/.env
    - 配置项：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID /
              AMAP_API_KEY / APP_HOST / APP_PORT / LOG_LEVEL
    - validate_config() 三层校验（需求文档「坑 4」）：
        1. 非空
        2. 前缀/长度合理性（高德 Key 为 32 位十六进制）
        3. 真实连通性探测（调高德地理编码查「北京」，检查 infocode == "10000"）
    - 启动时若检测到系统环境里存在 OPENAI_API_KEY，打印明确警告
      （不修改用户系统变量；需求文档「坑 2」）

注意：本项目不依赖 hello-agents，因此不存在 provider 被劫持的问题。d"""
"""配置加载与校验。

约定：只有本文件直接读环境变量，其他模块一律通过 get_settings() 取配置。
"""

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

import httpx
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
    #   False = 天气退化成「直接调一次工具」（0 token、1 次 HTTP）
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

    # ---- 服务 ----
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"


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
            ("AMAP_API_KEY", settings.amap_api_key),
        )
        if not value.strip()
    ]
    if missing:
        raise ConfigError(
            f"缺少必需的配置项：{'、'.join(missing)}；请检查 {ENV_FILE}"
        )

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


