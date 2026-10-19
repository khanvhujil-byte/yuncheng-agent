r"""Java + Python 跨服务联调与高并发验收脚本。

它验证的是一条完整的业务闭环，而不只是"两个服务都能起来"：

    1. Python 生成行程  →  交给 Java 发布（Java 持库）      [Python → Java]
    2. Java 发布时回调 Python 生成简介                      [Java → Python]
    3. 高并发点赞（N 个用户同时点）                          [Redis + Lua 原子]
    4. 异步落库后核对 MySQL 与 Redis 是否一致                 [最终一致]
    5. 读排行榜（含时间衰减）与热度统计                       [反哺 Python 的数据源]
    6. 反哺：把 Java 的热门数据喂给 Python 生成链路           [Java → Python 闭环]

用法（在 backend 目录下，或指定 --python 可执行文件）：
    .\.venv\Scripts\python.exe -u ..\scripts\interop_check.py --users 300

设计原则：**所有数字都是跑出来的**，脚本自己打印汇总表；任何一步不一致就退出码 1。
"""

import argparse
import asyncio
import json
import statistics
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

JAVA = "http://127.0.0.1:8080/community"
PYTHON = "http://127.0.0.1:8000"


def _plan(city: str, days: int) -> dict:
    """造一份 TripPlan（结构对齐 Python 的 schemas.py，两个服务共用同一套字段名）。"""
    start = date.today()
    return {
        "city": city,
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days - 1)).isoformat(),
        "days": [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "day_index": index,
                "description": f"第 {index + 1} 天",
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "attractions": [
                    {"name": f"{city}景点{index}-{j}", "address": "测试路", "visit_duration": 120}
                    for j in range(2)
                ],
                "meals": [{"type": "lunch", "name": "本地菜", "estimated_cost": 60}],
                "day_total_cost": 200,
            }
            for index in range(days)
        ],
        "budget": {"total": 1500},
        "overall_suggestions": "",
    }


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


async def fire_likes(client: httpx.AsyncClient, itinerary_id: int, user_ids: list[str],
                     concurrency: int = 64) -> tuple[list[float], dict[str, int]]:
    """并发点赞：N 个不同用户同时点同一条行程。"""
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    codes: dict[str, int] = {}

    async def one(user_id: str):
        async with semaphore:
            t0 = time.perf_counter()
            try:
                resp = await client.post(
                    f"{JAVA}/itineraries/{itinerary_id}/like",
                    headers={"X-Session-Token": user_id}, timeout=30)
                latency = (time.perf_counter() - t0) * 1000
                latencies.append(latency)
                body = resp.json()
                key = "ok" if body.get("success") else str(body.get("errorCode") or resp.status_code)
                codes[key] = codes.get(key, 0) + 1
            except Exception as exc:  # noqa: BLE001
                latencies.append((time.perf_counter() - t0) * 1000)
                key = type(exc).__name__
                codes[key] = codes.get(key, 0) + 1

    await asyncio.gather(*(one(uid) for uid in user_ids))
    return latencies, codes


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=300, help="并发点赞的不同用户数")
    parser.add_argument("--concurrency", type=int, default=64, help="同时在飞的请求数")
    parser.add_argument('--tokens-file', type=Path, required=True, help='JSON array: valid session tokens for users+2 distinct test accounts; never use production accounts')
    args = parser.parse_args()
    tokens=json.loads(args.tokens_file.read_text(encoding='utf-8'))
    if not isinstance(tokens,list) or len(tokens)<args.users+2 or not all(isinstance(t,str) and t for t in tokens):
        parser.error('tokens-file must contain at least users+2 valid sessions')

    failures: list[str] = []

    def require(label: str, ok: bool, detail: str = "") -> None:
        if not check(label, ok, detail):
            failures.append(label)

    async with httpx.AsyncClient() as client:
        identities=[]
        for token in tokens[:args.users+2]:
            response=await client.get(f'{PYTHON}/api/auth/community',headers={'X-Session-Token':token})
            response.raise_for_status()
            identities.append(response.json()['userId'])
        if len(set(identities))!=len(identities):
            parser.error('Each test token must represent a distinct account')
        # ---------------------------------------------------------- 0. 两个服务都活着
        print("=== 0) 服务健康 ===", flush=True)
        java_health = (await client.get(f"{JAVA}/health", timeout=10)).json()
        py_health = (await client.get(f"{PYTHON}/health", timeout=10)).json()
        require("Java 服务（MySQL + Redis）", java_health.get("status") == "ok", json.dumps(java_health, ensure_ascii=False))
        require("Python 服务", py_health.get("status") == "ok", f"version={py_health.get('version')}")

        # ---------------------------------------------------------- 1. Java → Python 回调
        print("\n=== 1) Java 回调 Python 生成简介（Java → Python）===", flush=True)
        summary_resp = await client.post(
            f"{PYTHON}/api/ai/summary",
            json={"city": "杭州", "travel_days": 2, "plan_json": json.dumps(_plan("杭州", 2), ensure_ascii=False)},
            timeout=60)
        require("回调接口可用", summary_resp.status_code == 200, f"HTTP {summary_resp.status_code}")
        summary_body = summary_resp.json() if summary_resp.status_code == 200 else {}
        print(f"     简介: {summary_body.get('summary', '')!r}", flush=True)
        print(f"     来源: {summary_body.get('source')}", flush=True)

        # ---------------------------------------------------------- 2. Python → Java 发布
        print("\n=== 2) 发布行程到 Java（Python → Java，Java 持库）===", flush=True)
        publish = await client.post(
            f"{JAVA}/itineraries",
            json={"title": "杭州两日自然风光", "city": "杭州", "travelDays": 2,
                  "startDate": date.today().isoformat(), "plan": _plan("杭州", 2)},
            headers={"X-Session-Token": tokens[0]}, timeout=90)
        require("发布成功", publish.status_code == 200, f"HTTP {publish.status_code}")
        published = publish.json()
        itinerary_id = published.get("itineraryId")
        require("拿到 itineraryId", isinstance(itinerary_id, int), f"id={itinerary_id}")
        if itinerary_id is None:
            print("\n发布失败，后续无法继续", flush=True)
            return 1

        detail = (await client.get(f"{JAVA}/itineraries/{itinerary_id}", timeout=20)).json()
        require("入库的行程能查回", detail.get("city") == "杭州", f"city={detail.get('city')}")
        require("plan 原样保存（跨语言契约没被翻译坏）",
                (detail.get("plan") or {}).get("days", [{}])[0].get("day_index") == 0,
                f"days={len((detail.get('plan') or {}).get('days') or [])}")
        require("AI 摘要已回填到行程里", bool(detail.get("aiSummary")),
                f"summary={detail.get('aiSummary', '')[:40]}")

        # ---------------------------------------------------------- 3. 幂等与限频
        print("\n=== 3) 幂等与防刷 ===", flush=True)
        first = (await client.post(f"{JAVA}/itineraries/{itinerary_id}/like",
                                   headers={"X-Session-Token": tokens[0]}, timeout=20)).json()
        require("首次点赞成功", first.get("liked") is True and first.get("likeCount") == 1,
                f"count={first.get('likeCount')}")
        again = (await client.post(f"{JAVA}/itineraries/{itinerary_id}/like",
                                   headers={"X-Session-Token": tokens[0]}, timeout=20)).json()
        require("重复点赞不重复计数（一人一赞）",
                again.get("success") is True and again.get("likeCount") == 1,
                f"count={again.get('likeCount')}")

        # 限频：同一个用户猛点 15 次，应该在第 11 次之后被拦
        limited = 0
        for _ in range(15):
            body = (await client.post(f"{JAVA}/itineraries/{itinerary_id}/like",
                                      headers={"X-Session-Token": tokens[1]}, timeout=20)).json()
            if body.get("errorCode") == "RATE_LIMITED":
                limited += 1
        require("单用户超频被拦", limited > 0, f"被拦 {limited} 次 / 共 15 次")

        # ---------------------------------------------------------- 4. 高并发点赞
        print(f"\n=== 4) 高并发点赞：{args.users} 个用户，并发 {args.concurrency} ===", flush=True)
        base_count = (await client.get(f"{JAVA}/itineraries/{itinerary_id}/like",
                                       timeout=20)).json().get("likeCount", 0)
        user_ids = tokens[2:2+args.users]

        t0 = time.perf_counter()
        latencies, codes = await fire_likes(client, itinerary_id, user_ids, args.concurrency)
        elapsed = time.perf_counter() - t0

        latencies.sort()

        def pct(p: float) -> float:
            return latencies[min(len(latencies) - 1, int(len(latencies) * p))] if latencies else 0.0

        qps = len(user_ids) / elapsed if elapsed > 0 else 0
        print(f"     耗时 {elapsed:.2f} 秒 | QPS {qps:,.0f} | p50 {pct(0.5):.1f}ms "
              f"| p95 {pct(0.95):.1f}ms | p99 {pct(0.99):.1f}ms", flush=True)
        print(f"     结果分布: {codes}", flush=True)

        after = (await client.get(f"{JAVA}/itineraries/{itinerary_id}/like", timeout=20)).json()
        expected = base_count + args.users
        require("Redis 计数无丢失（并发下不丢赞）",
                after.get("likeCount") == expected,
                f"期望 {expected}，实际 {after.get('likeCount')}")
        require("没有请求失败", codes.get("ok", 0) == args.users, f"成功 {codes.get('ok', 0)}/{args.users}")

        # ---------------------------------------------------------- 5. 异步落库一致性
        print("\n=== 5) 异步落库（等 3 秒让后台批量刷进去）===", flush=True)
        await asyncio.sleep(3)
        stats = (await client.get(f"{JAVA}/internal/like-stats", timeout=20)).json()
        print(f"     队列积压 {stats.get('pendingInQueue')} | 丢弃 {stats.get('dropped')}", flush=True)
        require("队列已排空", stats.get("pendingInQueue") == 0, f"pending={stats.get('pendingInQueue')}")
        require("没有因队列满而丢弃", stats.get("dropped") == 0, f"dropped={stats.get('dropped')}")

        detail_after = (await client.get(f"{JAVA}/itineraries/{itinerary_id}", timeout=20)).json()
        require("MySQL 里的计数与 Redis 一致（最终一致）",
                detail_after.get("likeCount") == expected,
                f"MySQL={detail_after.get('likeCount')} Redis={expected}")

        # ---------------------------------------------------------- 6. 排行榜
        print("\n=== 6) 排行榜（时间衰减）===", flush=True)
        board = (await client.get(f"{JAVA}/leaderboard", params={"type": "hot", "limit": 20}, timeout=20)).json()
        items = board.get("items") or []

        # ★ 断言的是**不变量**，不是"排第几"。
        #   原先这里写的是"目标行程必须是热榜第一" —— 它只在库里没有更热行程时成立，
        #   多次跑批攒下数据后必然失败。那是测试写脆了，不是产品坏了：
        #   系统从没承诺"新点的赞一定登顶"（时间衰减 + 已有票数共同决定名次）。
        entry = next((item for item in items if item.get("itineraryId") == itinerary_id), None)
        require("目标行程出现在热榜里", entry is not None,
                f"榜上 id = {[item.get('itineraryId') for item in items]}")
        if entry is not None:
            require("热榜里的点赞数与 Redis 一致", entry.get("likeCount") == expected,
                    f"榜上 {entry.get('likeCount')} vs 期望 {expected}")
        scores = [float(item.get("hotScore") or 0) for item in items]
        require("热榜按热度分降序", scores == sorted(scores, reverse=True),
                f"hotScore = {scores[:6]}")
        require("热榜里的行程都带 aiSummary（Java 回填的）",
                all(item.get("aiSummary") for item in items), "")
        if items:
            print(f"     榜首: {items[0].get('title')} | {items[0].get('likeCount')} 赞 "
                  f"| 热度 {items[0].get('hotScore')}", flush=True)
            print(f"     目标 #{itinerary_id} 名次: {[i.get('itineraryId') for i in items].index(itinerary_id) + 1}"
                  f"/{len(items)}", flush=True)

        newest = (await client.get(f"{JAVA}/leaderboard", params={"type": "new", "limit": 5}, timeout=20)).json()
        require("最新榜可用", newest.get("count", 0) >= 1, f"count={newest.get('count')}")

        # ---------------------------------------------------------- 7. 反哺 Python
        print("\n=== 7) 热度反哺（Java → Python 的闭环数据）===", flush=True)
        hot = (await client.get(f"{JAVA}/hot", params={"limit": 5}, timeout=20)).json()
        stats_map = hot.get("stats") or {}
        cities = [item["key"] for item in stats_map.get("city", [])]
        attractions = [item["key"] for item in stats_map.get("attraction", [])]
        print(f"     热门城市: {cities}", flush=True)
        print(f"     热门景点: {attractions[:5]}", flush=True)
        require("发布行程累加了城市热度", "杭州" in cities, f"cities={cities}")
        require("发布行程累加了景点热度（按天解析 plan）", len(attractions) > 0, f"{len(attractions)} 个")

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 62, flush=True)
    if failures:
        print(f"验收未通过，失败 {len(failures)} 项：", flush=True)
        for item in failures:
            print(f"  - {item}", flush=True)
        return 1
    print("全部通过：跨语言双向联动 + 高并发点赞 + 最终一致 + 反哺闭环", flush=True)
    print(f"并发数字：{args.users} 个用户 | QPS {qps:,.0f} | p50 {statistics.median(latencies):.1f}ms "
          f"| p95 {pct(0.95):.1f}ms | p99 {pct(0.99):.1f}ms", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
