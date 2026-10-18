"""Import maintainer-reviewed price observations. No network or model inference."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import price_catalog, store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="导入有来源、有适用日期和失效时间的门票/每晚房价/餐厅人均")
    parser.add_argument("file", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if args.file.stat().st_size > 1_000_000:
        parser.error("导入文件超过1MB")
    if args.db:
        store.DB_PATH = args.db.resolve()
    count = price_catalog.import_prices(json.loads(args.file.read_text(encoding="utf-8")))
    print(json.dumps({"imported": count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
