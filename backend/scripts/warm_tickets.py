"""Register reviewed official pages from a JSON file, refresh, report coverage."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import ticket_sources, store  # noqa: E402

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--registry', type=Path, help='JSON array of city,poi_id,name,url')
    parser.add_argument('--limit', type=int, default=3, choices=range(1,21))
    args = parser.parse_args()
    store.init_db()
    if args.registry:
        for entry in json.loads(args.registry.read_text(encoding='utf-8')):
            ticket_sources.register(**entry)
    print(json.dumps({'refresh':ticket_sources.refresh(args.limit),'coverage':ticket_sources.coverage()},ensure_ascii=False))
