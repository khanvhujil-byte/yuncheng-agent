"""Failure summaries and gate decisions must work without calling real agents."""
import sys

import pytest

from scripts import eval_gate


def test_all_cases_pass(monkeypatch, capsys):
    cases = eval_gate.build_cases()
    seen = []

    def run(case):
        seen.append(case.name)
        return True, []

    monkeypatch.setattr(sys, "argv", ["eval_gate.py", "--mode", "full"])
    monkeypatch.setattr(eval_gate, "build_cases", lambda: cases)
    monkeypatch.setattr(eval_gate, "run_case", run)
    assert eval_gate.main() == 0
    assert seen == [case.name for case in cases]
    output = capsys.readouterr().out
    assert "通过 7/7（100%）" in output
    assert "门禁通过" in output


@pytest.mark.parametrize("failed_index", range(7))
def test_each_current_correctness_case_can_veto_a_high_pass_rate(
    monkeypatch, capsys, failed_index
):
    cases = eval_gate.build_cases()
    failed_case = cases[failed_index]
    seen = []

    def run(case):
        seen.append(case.name)
        return (False, ["故障注入：不满足必须成立的规则"]) if case is failed_case else (True, [])

    monkeypatch.setattr(sys, "argv", ["eval_gate.py", "--mode", "full"])
    monkeypatch.setattr(eval_gate, "build_cases", lambda: cases)
    monkeypatch.setattr(eval_gate, "run_case", run)
    assert eval_gate.main() == 1
    assert seen == [case.name for case in cases]
    output = capsys.readouterr().out
    assert "通过 6/7（86%）" in output
    assert f"[FAIL] {failed_case.name}" in output
    assert "故障注入" in output
    assert "关键用例失败 → " + failed_case.name in output


@pytest.mark.parametrize(
    ("mode", "failed_count", "raises", "critical", "expected_code", "expected_summary"),
    [
        pytest.param("full", 1, False, False, 0, "通过 6/7（86%）", id="ordinary-failure-within-threshold"),
        pytest.param("full", 2, False, False, 1, "通过 5/7（71%）", id="below-threshold"),
        pytest.param("fast", 1, False, False, 1, "通过 5/6（83%）", id="fast-requires-all-selected-cases"),
        pytest.param("full", 1, True, True, 1, "通过 6/7（86%）", id="exception-does-not-stop-batch"),
    ],
)
def test_thresholds_filtering_and_exceptions(
    monkeypatch, capsys, mode, failed_count, raises, critical, expected_code, expected_summary
):
    cases = [eval_gate.Case(
        name=f"case-{index}", kind="generate" if index == 6 else "preflight",
        request={}, critical=critical and index == 0,
    ) for index in range(7)]
    seen = []

    def run(case):
        seen.append(case.name)
        if int(case.name.split("-")[1]) < failed_count:
            if raises:
                raise RuntimeError("mock evaluator timeout")
            return False, ["mock unmet expectation"]
        return True, []

    monkeypatch.setattr(sys, "argv", ["eval_gate.py", "--mode", mode])
    monkeypatch.setattr(eval_gate, "build_cases", lambda: cases)
    monkeypatch.setattr(eval_gate, "run_case", run)
    assert eval_gate.main() == expected_code
    selected = cases if mode == "full" else cases[:-1]
    assert seen == [case.name for case in selected]
    output = capsys.readouterr().out
    assert expected_summary in output
    if raises:
        assert "异常 RuntimeError: mock evaluator timeout" in output
    if expected_code:
        assert "门禁不通过" in output
    else:
        assert "[FAIL] case-0" in output
        assert "门禁通过" in output


def test_empty_selection_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["eval_gate.py", "--mode", "fast"])
    monkeypatch.setattr(eval_gate, "build_cases", lambda: [])
    monkeypatch.setattr(eval_gate, "run_case", lambda _case: pytest.fail("No case should run"))
    assert eval_gate.main() == 1
    assert "通过 0/0（0%）" in capsys.readouterr().out
