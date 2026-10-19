"""Populate real local POIs/forecasts through the configured paced map provider."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import catalog_refresh, store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="预热城市资料库及天气，不调用模型、不生成虚构报价")
    parser.add_argument("--cities", nargs="+", help="城市列表，例如 杭州 北京")
    parser.add_argument("--kinds", nargs="+", choices=catalog_refresh.KINDS, default=list(catalog_refresh.KINDS))
    parser.add_argument("--force", action="store_true", help="忽略计划更新时间；仍遵守执行锁和上游限流")
    parser.add_argument("--status", action="store_true", help="只查看最近更新任务")
    parser.add_argument("--db", type=Path, help="可选独立数据库")
    args = parser.parse_args()
    if args.db:
        store.DB_PATH = args.db.resolve()
    if args.status:
        result = catalog_refresh.status()
    else:
        if not args.cities:
            parser.error("请指定 --cities 或 --status")
        result = [catalog_refresh.refresh_city(city, kinds=args.kinds, force=args.force) for city in args.cities]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.status and any(job.get("status") == "error" for city in result for job in city["jobs"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
