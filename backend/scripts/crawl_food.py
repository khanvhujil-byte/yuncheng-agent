"""Bounded public AMap ranking import. Run from backend; no API key needed."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import ingestion, store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="采集高德公开美食榜单，遵守 robots，单页最多20条")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="公开榜单完整 URL，例如 https://www.amap.com/ranking/beijing/food")
    source.add_argument("--import-json", type=Path, help="导入之前采集的有来源 JSON")
    parser.add_argument("--city", help="城市中文名，必须与页面美食榜标题匹配")
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 21))
    parser.add_argument("--db", type=Path, help="可选独立数据库；默认使用项目数据库")
    parser.add_argument("--output", type=Path, help="另存带来源的 JSON")
    args = parser.parse_args()
    if args.db:
        store.DB_PATH = args.db.resolve()
    if args.import_json:
        if args.import_json.stat().st_size > ingestion.MAX_BYTES:
            parser.error("导入文件超过1MB")
        result = json.loads(args.import_json.read_text(encoding="utf-8"))
        result["stored"] = ingestion.import_ranking(result)
    else:
        if not args.city:
            parser.error("在线采集必须指定 --city")
        result = ingestion.crawl_ranking(args.url, city=args.city, limit=args.limit)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
