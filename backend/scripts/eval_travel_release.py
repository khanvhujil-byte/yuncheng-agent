"""Live travel regressions, one isolated process/database per public test case.

Run: python scripts/eval_travel_release.py --out experiments/release --only G22,G32,T01
Requires configured providers; reports real failures and never mocks live IO.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import eval_scenarios_30 as base  # noqa: E402


def cases():
    return base.build_cases() + [
        base.Case('T01', '丽江5天其中两天周边雪山', 'generate',
                  base._req('丽江', 5, transportation='自驾', preferences=['自然风光'],
                            free_text_input='有两天想去周边的雪山，另外三天在市区，不要太赶'),
                  {'success': True, 'days': 5}),
        base.Case('T02', '成都4天两天周边雪山公共交通可行性', 'generate',
                  base._req('成都', 4, preferences=['自然风光'], free_text_input='有两天想去周边的雪山'),
                  {'days': 4}),
        base.Case('T03', '杭州3天两天博物馆', 'generate',
                  base._req('杭州', 3, free_text_input='有两天逛博物馆，剩下一天市区休闲'),
                  {'success': True, 'days': 3}),
        base.Case('T04', '青岛3天其中两天海边', 'generate',
                  base._req('青岛', 3, preferences=['自然风光'], free_text_input='有两天去海边，想轻松一点'),
                  {'success': True, 'days': 3}),
        base.Case('T05', '主题天数超旅行天数在模型前拒绝', 'preflight',
                  base._req('丽江', 1, free_text_input='有两天想去周边的雪山'),
                  {'success': False, 'error_code': 'CONSTRAINT_INFEASIBLE'}),
        base.Case('T06', '成都4天两天周边雪山自驾分段评估', 'generate',
                  base._req('成都', 4, transportation='自驾', preferences=['自然风光'],
                            free_text_input='两天在周边雪山游玩，其余市区，住宿根据当晚和第二天动线灵活安排，避免远途折返'),
                  {'success': True, 'days': 4}),
        base.Case('T07', '杭州3天市内慢游必须连住', 'generate',
                  base._req('杭州', 3, free_text_input='只在市区慢游，不要换酒店，不去远郊',
                            requirements={'hotel':{'stay_policy':'must_same'}}),
                  {'success': True, 'days': 3}),
        base.Case('T08', '杭州晚到早离保留中间完整活动日', 'generate',
                  base._req('杭州', 3, transportation='自驾', arrival_time='22:00', departure_time='09:00',
                            free_text_input='第一天22点才到，第三天9点离开，只在中间一天市区休闲，其余安排接驳住宿'),
                  {'success': True, 'days': 3}),
    ]


def run_child(case, output, seed_db):
    output.mkdir(parents=True, exist_ok=True)
    source_hashes = {str(p.relative_to(BACKEND)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (BACKEND/'app').rglob('*.py')}
    (output/'source-manifest.json').write_text(json.dumps(source_hashes, indent=2), encoding='utf-8')
    logging.basicConfig(filename=output/'provider.log', level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    # Public test requests only. The local snapshot is never sent to providers;
    # it supplies the existing POI catalog, not previous conversation messages.
    base.REAL_DB = seed_db
    from app.services import store
    store.DB_PATH = base.prepare_eval_db(output)
    store.init_db()
    from app.agents.graph import app as graph
    from app.services import planning
    result = base.run_case(case, graph, planning, store)
    # Independent public-name oracle for the added semantic regressions. It
    # does not ask the generating model whether its own answer is satisfactory.
    rules = {'T01': ('雪山', '蓝月谷', '云杉坪', '牦牛坪', '冰川公园'),
             'T02': ('雪山', '四姑娘山', '达古冰川', '贡嘎'),
             'T06': ('雪山', '四姑娘山', '达古冰川', '贡嘎'),
             'T03': ('博物馆', '科技馆', '美术馆'),
             'T04': ('海滩', '沙滩', '海滨', '海岸', '浴场', '海湾')}
    if case.id in rules:
        matching_days = {day['day_index'] for day in result.response_summary.get('days', [])
                         if any(any(word in name for word in rules[case.id]) for name in day['attractions'])}
        if len(matching_days) < 2:
            result.violations.append('独立主题验收：至少2个不同日期出现相关真实景点，实际'+str(len(matching_days)))
            result.passed = False
    if case.id == 'T07':
        ids = {s['hotel'].get('poi_id') or s['hotel']['name'] for s in result.response_summary.get('stays') or []}
        if len(ids) != 1:
            result.violations.append('市内必须连住验收：应恰好一家真实酒店')
            result.passed = False
    if case.id == 'T08':
        counts = [len(d['meals']) for d in result.response_summary.get('days', [])]
        if counts != [0,3,1]:
            result.violations.append('晚到早离独立验收：餐次数应为[0,3,1]，实际'+str(counts))
            result.passed = False
    (output/'result.json').write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding='utf-8')


def launch(case, output, seed_db, timeout):
    folder = output/case.id
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-u', str(Path(__file__).resolve()), '--child', case.id,
               '--out', str(folder), '--seed-db', str(seed_db)]
    started = time.perf_counter()
    with (folder/'process.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=BACKEND, stdout=log, stderr=subprocess.STDOUT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.kill()
            process.wait(timeout=15)
    result_file = folder/'result.json'
    if result_file.exists():
        result = json.loads(result_file.read_text(encoding='utf-8'))
    else:
        result = {'case_id': case.id, 'name': case.name, 'kind': case.kind, 'passed': False,
                  'success': None, 'days': 0, 'duration_s': round(time.perf_counter()-started, 2),
                  'violations': ['隔离进程超时或异常退出，见process.log'], 'request': case.request,
                  'exception': 'PROCESS_EXIT_OR_TIMEOUT'}
    summary = {k: result.get(k) for k in ('case_id', 'passed', 'success', 'duration_s')}
    summary['violations'] = [str(v)[:180] for v in result.get('violations', [])[:4]]
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seed-db', type=Path, default=BACKEND/'data/trip_planner.db')
    parser.add_argument('--only', default='')
    parser.add_argument('--child')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    selected = cases()
    if args.child:
        run_child(next(c for c in selected if c.id == args.child), args.out, args.seed_db)
        return 0
    wanted = {c.strip() for c in args.only.split(',') if c.strip()}
    if wanted:
        known = {c.id for c in selected}
        if wanted-known:
            parser.error('Unknown cases: '+','.join(sorted(wanted-known)))
        selected = [c for c in selected if c.id in wanted]
    args.out.mkdir(parents=True, exist_ok=True)
    source_hashes = {str(p.relative_to(BACKEND)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (BACKEND/'app').rglob('*.py')}
    (args.out/'source-manifest.json').write_text(json.dumps(source_hashes, indent=2), encoding='utf-8')
    results = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(launch, c, args.out, args.seed_db, args.timeout) for c in selected]
        for future in as_completed(futures):
            results.append(future.result())
            report = {'mode': 'live_providers_isolated_processes', 'generated_at': base.datetime.now().isoformat(),
                      'elapsed_seconds': round(time.perf_counter()-started, 2),
                      'cases': sorted(results, key=lambda r: r['case_id'])}
            (args.out/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return int(any(not r['passed'] for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
