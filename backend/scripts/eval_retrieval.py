"""Measure local retrieval against reviewed POI labels, with no network/model IO.

Input: [{city,kind,query,relevant_ids:[...],usable_ids:[...]}]. Run against a
snapshot database. Never describe a synthetic fixture score as live recall.
"""
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import catalog, store  # noqa: E402


def evaluate(cases, k=10):
    if not cases:
        raise ValueError('A non-empty labeled query set is required')
    rows=[]
    for case in cases:
        expected=set(case['relevant_ids'])
        if not expected:
            raise ValueError('Each query must have reviewed relevant IDs')
        usable=set(case.get('usable_ids') or expected)
        started=perf_counter()
        places, _ = catalog.search_places(case['city'],case['kind'],query=case['query'],limit=k)
        ids={p['poi_id'] for p in places}
        rows.append({'city':case['city'],'kind':case['kind'],'query':case['query'],
                     'recall_at_k':len(ids & expected)/len(expected),'usable_hit':bool(ids & usable),
                     'returned_count':len(ids),'ms':round((perf_counter()-started)*1000,2)})
    return {'samples':len(rows),'k':k,'mean_recall':sum(r['recall_at_k'] for r in rows)/len(rows),
            'usable_query_coverage':sum(r['usable_hit'] for r in rows)/len(rows),'queries':rows}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('labels',type=Path)
    parser.add_argument('--db',type=Path,required=True,help='Snapshot of catalog database')
    parser.add_argument('--k',type=int,choices=range(1,101),default=10)
    args=parser.parse_args()
    if not args.db.is_file():
        parser.error('Snapshot database does not exist')
    store.DB_PATH=args.db
    print(json.dumps(evaluate(json.loads(args.labels.read_text(encoding='utf-8')),args.k),ensure_ascii=False,indent=2))
