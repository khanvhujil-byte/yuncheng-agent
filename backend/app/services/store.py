"""行程库与运行日志（SQLite，标准库 sqlite3，**零新依赖**）。

对应 learn-claude-code 课程里的两章：
    s09 Memory        —— selection（找得到）/ extraction（存什么）/ consolidation（复用与统计）
    s10 Task System   —— 结果**落盘持久化**，而不是只活在内存里

它解决什么问题：
    v0.3 一次规划要 40 秒、11 次 LLM 调用、1.8 万 token。
    但**同一批需求的第二次请求，完全没必要重算** —— 用户在页面上改个出发日期再点一次，
    拿到的应该是"把日期平移一下"的行程，而不是又等 40 秒。

三个设计决定（都是踩过坑之后的取舍）：

    1. **指纹不含出发日期**。同一城市、同天数、同偏好、同必去景点的行程骨架是可复用的；
       日期差异用「**平移复用**」处理（把 days[].date 整体平移 + 按新日期重取天气）。
       所以指纹刻意排除 start_date / end_date，只保留"需求形状"。

    2. **命中后仍然重取天气**。天气是跟着日期走的，直接复用旧响应会把"10 月 10 日晴"
       搬到 11 月，那是**编造**。所以命中路径也要调一次高德天气（0 token，2 次 HTTP）。
       这条是诚实性约束的直接推论：**能复用骨架，不能复用跟时间绑定的外部数据**。

    3. **每次运行都记一行 run_log**。它让"缓存命中率 / 平均耗时 / token 成本"这些数字
       可以直接 SQL 查出来，不用回头翻日志。
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..models.schemas import TripRequest

logger = logging.getLogger(__name__)

# 数据库放在 backend/data/ 下。用 SQLite 而不是 Postgres 的理由：
# 这是单机单用户的简历项目，SQLite 零运维、零依赖、可直接拷走当证据。
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "trip_planner.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS itinerary (
    fingerprint   TEXT PRIMARY KEY,   -- 需求形状的指纹（不含日期）
    city          TEXT NOT NULL,
    travel_days   INTEGER NOT NULL,
    request_json  TEXT NOT NULL,      -- 原始请求，便于人工核对"为什么算相似"
    plan_json     TEXT NOT NULL,      -- 完整 TripPlan（不含日期相关字段的天气）
    created_at    TEXT NOT NULL,
    hit_count     INTEGER NOT NULL DEFAULT 0,
    last_hit_at   TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id     TEXT,
    fingerprint   TEXT,
    cached        INTEGER NOT NULL DEFAULT 0,   -- 1 = 走缓存命中路径（没花 LLM 钱）
    success       INTEGER NOT NULL DEFAULT 0,
    error_code    TEXT,
    elapsed_ms    INTEGER NOT NULL DEFAULT 0,
    llm_calls     INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    plan_days     INTEGER NOT NULL DEFAULT 0,
    budget_total  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_log_created ON run_log(created_at);

-- v0.5：后台任务状态外置（解决 12-Factor 的 F12「agent 应该是无状态 reducer」）
-- 内存里的 _jobs 只当缓存；**真正的状态在这里** —— 换副本或进程重启后仍能取到结果与进度。
CREATE TABLE IF NOT EXISTS job_state (
    thread_id     TEXT PRIMARY KEY,
    status        TEXT NOT NULL,              -- running / done / error
    events_json   TEXT NOT NULL DEFAULT '[]', -- 已产生的进度事件（SSE 重放用）
    result_json   TEXT,                       -- 完成后的 TripPlanResponse
    error         TEXT NOT NULL DEFAULT '',
    started_at    REAL NOT NULL DEFAULT 0,
    finished_at   REAL,
    updated_at    TEXT NOT NULL
);

-- v0.4：后台生成任务与节点级 journal（对应 learn-claude-code 的 s11 后台任务 / s16 workflow journal）
CREATE TABLE IF NOT EXISTS graph_run (
    thread_id     TEXT PRIMARY KEY,
    request_json  TEXT NOT NULL,   -- 原样存下来，续跑时才能重建初始 state
    journal_version INTEGER NOT NULL DEFAULT 2,
    created_at    TEXT NOT NULL
);

-- 旧版覆盖式日志只保留供查看，不能用于可靠恢复。
CREATE TABLE IF NOT EXISTS graph_journal (
    thread_id     TEXT NOT NULL,
    node          TEXT NOT NULL,
    seq           INTEGER NOT NULL,          -- 节点执行顺序，用于判断"第一个没做的节点"
    update_json   TEXT NOT NULL,             -- 该节点写回 state 的那部分（原样存）
    elapsed_ms    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (thread_id, node)
);

-- 每次节点执行单独留一条，不允许覆盖已提交的执行。
CREATE TABLE IF NOT EXISTS graph_journal_event (
    thread_id     TEXT NOT NULL,
    node          TEXT NOT NULL,
    seq           INTEGER NOT NULL CHECK (seq > 0),
    update_json   TEXT NOT NULL,
    elapsed_ms    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (thread_id, seq)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_default(obj: Any) -> Any:
    """把 Pydantic 模型转成 dict，供 json.dumps 使用。

    为什么需要它（真实踩过）：`TripPlan` 是**混合体** ——
        days / budget / 来源标记  是普通 dict（来自 compose_plan 与 patch_budget）
        weather_info / hotel / constraint_report  直接来自 state，是 **Pydantic 对象**
    直接 json.dumps 会在 WeatherInfo 上抛
    "TypeError: Object of type WeatherInfo is not JSON serializable" —— 而且是在
    行程**已经生成完**（花了 40 秒与 LLM 费用）之后的入库那一刻才炸，最亏的一种失败方式。
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return str(obj)


def _connect() -> sqlite3.Connection:
    """每次调用开一个新连接。

    为什么不用全局连接：FastAPI 的同步端点跑在线程池里，SQLite 连接默认不能跨线程。
    开连接的开销在这个量级（每次请求个位数次）完全可以忽略，省掉一整类线程安全问题。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表。幂等，进程启动时调一次即可。"""
    with _connect() as conn:
        from .dialog_store import SCHEMA as dialog_schema
        conn.executescript(SCHEMA + dialog_schema)
        # ALTER 的默认值必须是 1：历史任务可能已被覆盖，不能冒充完整日志。
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(graph_run)")}
        if "journal_version" not in columns:
            conn.execute("ALTER TABLE graph_run ADD COLUMN journal_version INTEGER NOT NULL DEFAULT 1")
    logger.info("行程库就绪 | %s", DB_PATH)


def fingerprint_of(req: TripRequest) -> str:
    """需求形状的指纹 —— **刻意不含日期**（理由见模块 docstring 第 1 条）。

    规范化后再哈希：
        - 偏好标签、必去景点都**排序**：勾选顺序不同不影响是不是同一批需求
        - 字符串 strip：多打一个空格不该算另一批需求
    用 sha256 而不是 Python 的 hash()：后者每次进程重启都会变，没法落盘复用。
    """
    nights = req.nights if req.nights is not None else max(req.travel_days - 1, 0)
    payload = {
        "city": req.city.strip(),
        "travel_days": req.travel_days,
        "nights": nights,
        "transportation": req.transportation.strip(),
        "accommodation": req.accommodation.strip(),
        "preferences": sorted(p.strip() for p in (req.preferences or []) if p.strip()),
        "must_visit": sorted(m.strip() for m in (req.must_visit or []) if m.strip()),
        "fixed_hotel": (req.fixed_hotel or "").strip(),
        "fixed_hotel_address": (req.fixed_hotel_address or "").strip(),
        "contract_version": 4,
        "requirements": req.requirements.model_dump(),
        "free_text_input": (req.free_text_input or "").strip(),
        # ★ 预算上限必须进指纹：它是**约束**，不是展示字段。
        #   不写进来的话，"预算 2000 的杭州 3 天"会命中"预算不限的杭州 3 天"，
        #   把一份可能超限的行程当成合格答案返回 —— 这是最典型的缓存语义错误。
        "budget_limit": req.budget_limit,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_itinerary(fingerprint: str, req: TripRequest, plan: dict[str, Any]) -> None:
    """写入/覆盖一条行程。已存在就刷新内容，但**保留 hit_count**（它是历史复用次数）。"""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO itinerary (fingerprint, city, travel_days, request_json, plan_json, created_at, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(fingerprint) DO UPDATE SET
                plan_json  = excluded.plan_json,
                request_json = excluded.request_json,
                created_at = excluded.created_at
            """,
            (
                fingerprint,
                req.city,
                req.travel_days,
                json.dumps(req.model_dump(), ensure_ascii=False),
                json.dumps(plan, ensure_ascii=False, default=_json_default),
                _now(),
            ),
        )
    logger.info("行程已入库 | fingerprint=%s | %s %d 天", fingerprint[:12], req.city, req.travel_days)


def find_itinerary(fingerprint: str) -> Optional[dict[str, Any]]:
    """按指纹找行程。命中就顺手 +1（复用次数是缓存价值最直接的证据）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT plan_json, created_at, hit_count FROM itinerary WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE itinerary SET hit_count = hit_count + 1, last_hit_at = ? WHERE fingerprint = ?",
            (_now(), fingerprint),
        )
    plan = json.loads(row["plan_json"])
    logger.info("行程命中缓存 | fingerprint=%s | 建于 %s | 第 %d 次复用",
                fingerprint[:12], row["created_at"], row["hit_count"] + 1)
    return {"plan": plan, "created_at": row["created_at"], "hit_count": row["hit_count"] + 1}


def log_run(
    *,
    thread_id: str,
    fingerprint: str,
    cached: bool,
    success: bool,
    error_code: Optional[str] = None,
    elapsed_ms: int = 0,
    llm_calls: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    tool_calls: int = 0,
    plan_days: int = 0,
    budget_total: int = 0,
) -> None:
    """每次请求记一行。让"缓存命中率 / 平均耗时 / token 成本"可以直接 SQL 查出来。"""
    with _connect() as conn:
        inserted = conn.execute("INSERT OR IGNORE INTO completed_run(run_id) VALUES(?)", (thread_id,))
        if not inserted.rowcount:
            return
        conn.execute(
            """
            INSERT INTO run_log (thread_id, fingerprint, cached, success, error_code, elapsed_ms,
                                 llm_calls, prompt_tokens, completion_tokens, tool_calls,
                                 plan_days, budget_total, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (thread_id, fingerprint, int(cached), int(success), error_code, elapsed_ms,
             llm_calls, prompt_tokens, completion_tokens, tool_calls,
             plan_days, budget_total, _now()),
        )


def save_run(thread_id: str, req: TripRequest) -> None:
    """记下这次后台任务用的原始请求 —— 续跑时要靠它重建初始 state。"""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO graph_run (thread_id, request_json, created_at, journal_version) VALUES (?, ?, ?, 2)
            ON CONFLICT(thread_id) DO UPDATE SET request_json = excluded.request_json
            """,
            (thread_id, json.dumps(req.model_dump(), ensure_ascii=False), _now()),
        )


def load_run(thread_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT request_json, created_at, journal_version FROM graph_run WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    if row is None:
        return None
    return {"request": json.loads(row["request_json"]), "created_at": row["created_at"],
            "journal_version": row["journal_version"]}


def journal_node(thread_id: str, node: str, seq: int, update: dict[str, Any], elapsed_ms: int = 0) -> None:
    """把某个节点写回 state 的那部分落盘（**原样存**，不解析、不裁剪）。

    为什么要原样存：续跑时要把这些碎片重新拼成一个可用的 state，
    任何"为了好看"的转换都会在拼回去时变成 bug。
    """
    with _connect() as conn:
        run = conn.execute("SELECT journal_version FROM graph_run WHERE thread_id = ?", (thread_id,)).fetchone()
        if run is None or run["journal_version"] != 2:
            raise ValueError("只有新版已登记任务可以追加执行日志")
        conn.execute(
            """
            INSERT INTO graph_journal_event (thread_id, node, seq, update_json, elapsed_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (thread_id, node, seq,
             json.dumps(update, ensure_ascii=False, default=_json_default), elapsed_ms, _now()),
        )


def load_journal(thread_id: str) -> list[dict[str, Any]]:
    """按执行顺序返回该任务的节点记录。"""
    with _connect() as conn:
        run = conn.execute("SELECT journal_version FROM graph_run WHERE thread_id = ?", (thread_id,)).fetchone()
        table = "graph_journal" if run and run["journal_version"] == 1 else "graph_journal_event"
        rows = conn.execute(
            f"SELECT node, seq, update_json, elapsed_ms, created_at FROM {table} "
            "WHERE thread_id = ? ORDER BY seq",
            (thread_id,),
        ).fetchall()
    return [
        {
            "node": row["node"],
            "seq": row["seq"],
            "update": json.loads(row["update_json"]),
            "elapsed_ms": row["elapsed_ms"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def save_job(
    thread_id: str,
    *,
    status: str,
    events: list[dict[str, Any]],
    result: Optional[dict[str, Any]] = None,
    error: str = "",
    started_at: float = 0.0,
    finished_at: Optional[float] = None,
) -> None:
    """把后台任务的状态写进库（每个进度事件后都刷一次）。

    为什么每个事件都写：SSE 是"随时可能断"的。断线重连后要能把已发生的事件重放出来，
    所以事件必须落盘，不能只活在内存里。写放大是每请求个位数次，SQLite 完全扛得住。
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO job_state (thread_id, status, events_json, result_json, error,
                                   started_at, finished_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                status      = excluded.status,
                events_json = excluded.events_json,
                result_json = excluded.result_json,
                error       = excluded.error,
                finished_at = excluded.finished_at,
                updated_at  = excluded.updated_at
            """,
            (
                thread_id,
                status,
                json.dumps(events, ensure_ascii=False, default=_json_default),
                json.dumps(result, ensure_ascii=False, default=_json_default) if result else None,
                error,
                started_at,
                finished_at,
                _now(),
            ),
        )


def load_job(thread_id: str) -> Optional[dict[str, Any]]:
    """从库里读回任务状态（内存里没有时用它兜底）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status, events_json, result_json, error, started_at, finished_at, updated_at "
            "FROM job_state WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "status": row["status"],
        "events": json.loads(row["events_json"] or "[]"),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"] or "",
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
    }


def stats() -> dict[str, Any]:
    """给 /api/trip/stats 用：一眼看出缓存有没有真的在省事。"""
    with _connect() as conn:
        runs = conn.execute(
            """
            SELECT
                COUNT(*)                                             AS total_runs,
                SUM(cached)                                          AS cached_runs,
                AVG(CASE WHEN cached = 0 THEN elapsed_ms END)        AS avg_ms_generated,
                AVG(CASE WHEN cached = 1 THEN elapsed_ms END)        AS avg_ms_cached,
                SUM(CASE WHEN cached = 0 THEN llm_calls END)         AS llm_calls_generated,
                SUM(CASE WHEN cached = 0 THEN prompt_tokens + completion_tokens END) AS tokens_generated,
                SUM(success)                                         AS successes
            FROM run_log
            """
        ).fetchone()
        stored = conn.execute("SELECT COUNT(*) AS n, SUM(hit_count) AS hits FROM itinerary").fetchone()

    def _r(value: Optional[float], digits: int = 1) -> Optional[float]:
        return round(value, digits) if value is not None else None

    total = runs["total_runs"] or 0
    cached = runs["cached_runs"] or 0
    return {
        "total_runs": total,
        "cached_runs": cached,
        "cache_hit_rate": round(cached / total, 3) if total else 0.0,
        "itineraries_stored": stored["n"] or 0,
        "itineraries_reused": stored["hits"] or 0,
        "avg_ms_generated": _r(runs["avg_ms_generated"], 0),
        "avg_ms_cached": _r(runs["avg_ms_cached"], 0),
        "llm_calls_generated": runs["llm_calls_generated"] or 0,
        "tokens_generated": runs["tokens_generated"] or 0,
    }
