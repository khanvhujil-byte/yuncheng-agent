"""Reproducible offline integrated evaluation; never calls paid providers.

Run from backend: python scripts/eval_integrated.py --output eval-integrated.json
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

import pytest


class Results:
    def __init__(self):
        self.cases = {}

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.failed:
            self.cases[report.nodeid] = {'outcome': report.outcome, 'seconds': round(report.duration, 4)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='eval-integrated.json')
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    collector = Results()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='trip-eval-') as temporary:
        cases = ['test_integrated_e2e.py', 'test_dialogue_upgrade.py', 'test_knowledge_monitor_upgrade.py',
                 'test_mcp_upgrade.py', 'test_itinerary_experience.py', 'test_partial_edit.py',
                 'test_food_catalog_ingestion.py', 'test_price_catalog.py', 'test_price_integration.py',
                 'test_food_runtime.py', 'test_requirements.py']
        code = pytest.main([*[str(backend/'tests'/name) for name in cases],
                            '-q', '-p', 'no:cacheprovider', '-o', 'addopts=--strict-markers', '--basetemp', temporary], plugins=[collector])
    report = {'mode': 'offline_mocked_external_io', 'contract_version': 4,
              'elapsed_seconds': round(time.perf_counter()-started, 3), 'exit_code': int(code),
              'passed': sum(r['outcome'] == 'passed' for r in collector.cases.values()),
              'failed': sum(r['outcome'] == 'failed' for r in collector.cases.values()),
              'cases': collector.cases,
              'limits': 'Correctness regressions with fixture I/O; not live provider quality, latency or production success rates.'}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return int(code)


if __name__ == '__main__':
    raise SystemExit(main())
