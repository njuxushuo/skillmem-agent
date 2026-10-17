"""Offline verification of the shared Stage 5 paid-experiment guard and exports."""

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("benchmark", SCRIPTS / "run_benchmark.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
sys.path.pop(0)


def ledger_at(tmp_path):
    return bench.BudgetLedger(tmp_path / "budget.json", limits=bench.CONFIG["limits"])


def test_parallel_reservations_and_restart_never_exceed_three_dollars(tmp_path):
    path = tmp_path / "budget.json"
    ledger_at(tmp_path)

    def attempt(i):
        ledger = bench.BudgetLedger(path, limits=bench.CONFIG["limits"])
        try:
            return ledger.reserve(str(i).encode())
        except bench.BudgetStopped:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        indices = list(pool.map(attempt, range(32)))
    assert sorted(i for i in indices if i is not None) == list(range(9))
    summary = ledger_at(tmp_path).summary()
    assert summary["http_attempts"] == 9
    assert Decimal(summary["cost_upper_usd"]) == Decimal("2.8422144")
    with pytest.raises(bench.BudgetStopped):
        ledger_at(tmp_path).reserve(b"restart cannot reset")


def test_physical_request_2000_is_final_allowed_request(tmp_path):
    ledger = ledger_at(tmp_path)
    with ledger.edit() as state:
        state["attempts"] = [{"upper_usd": "0", "status": "response"} for _ in range(1999)]
    assert ledger.reserve(b"2000") == 1999
    with pytest.raises(bench.BudgetStopped):
        ledger.reserve(b"2001")
    assert ledger.summary()["http_attempts"] == 2000


@pytest.mark.parametrize(
    "changes",
    [{"requests": 2001}, {"usd": "3.01"}, {"input_rate": "0.1"}, {"context_limit": 1000}, {"output_limit": 2048}],
)
def test_unsafe_limits_fail_before_ledger_or_transport(tmp_path, changes):
    limits = {**bench.CONFIG["limits"], **changes}
    with pytest.raises(ValueError):
        bench.BudgetLedger(tmp_path / "budget.json", limits=limits)
    assert not (tmp_path / "budget.json").exists()


def test_phase_claim_is_single_use_and_shared_across_restarts(tmp_path):
    ledger = ledger_at(tmp_path)
    with pytest.raises(bench.BudgetStopped, match="pilot_not_validated"):
        bench.claim_phase(ledger, "final", "hash")
    bench.claim_phase(ledger, "pilot", "hash")
    index = ledger.reserve(b"pilot")
    ledger.settle(index, http_status=200, usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
    with pytest.raises(bench.BudgetStopped, match="phase_already_claimed"):
        bench.claim_phase(ledger_at(tmp_path), "pilot", "hash")
    with ledger.edit() as state:
        state["phases"]["pilot"]["validated"] = True
    with pytest.raises(bench.BudgetStopped, match="protocol_changed"):
        bench.claim_phase(ledger, "final", "different")
    bench.claim_phase(ledger_at(tmp_path), "final", "hash")
    assert ledger.reserve(b"final") == 1
    assert ledger.summary()["http_attempts"] == 2
    with pytest.raises(bench.BudgetStopped):
        bench.claim_phase(ledger, "final", "hash")


def test_unknown_usage_and_error_do_not_release_cost(tmp_path):
    ledger = ledger_at(tmp_path)
    i = ledger.reserve(b"unknown")
    ledger.settle(i, http_status=200, usage={"prompt_tokens": 0})
    j = ledger.reserve(b"api_error")
    ledger.settle(j, http_status=500)
    assert Decimal(ledger.summary()["cost_upper_usd"]) == ledger.reservation * 2
    with pytest.raises(bench.BudgetStopped):
        ledger.reserve(b"after_error")


def test_changed_utc_pricing_date_stops_before_send(tmp_path):
    limits = {**bench.CONFIG["limits"], "pricing_verified_on": "2000-01-01"}
    ledger = bench.BudgetLedger(tmp_path / "budget.json", limits=limits)
    transport = bench.DatedTransport(ledger, httpx.MockTransport(lambda r: pytest.fail("unexpected send")))
    with pytest.raises(bench.BudgetStopped, match="pricing_review"):
        transport.handle_request(httpx.Request("POST", "https://api.deepseek.com/chat/completions", json={}))
    assert ledger.summary()["http_attempts"] == 0


def fixture_root(tmp_path):
    for directory in ("data", "src/support_agent", "tests", "scripts"):
        (tmp_path / directory).mkdir(parents=True)
    for name in (
        "requirements.lock",
        "pyproject.toml",
        ".python-version",
        "data/tasks.json",
        "src/support_agent/graph.py",
    ):
        (tmp_path / name).write_text("frozen")
    return tmp_path


@pytest.mark.parametrize("mutation", ["source", "new_file", "config", "manifest"])
def test_protocol_freeze_rejects_tampering(tmp_path, monkeypatch, mutation):
    root = fixture_root(tmp_path)
    manifest = bench.freeze(root, root / ".local")
    assert bench.verify_manifest(root) == manifest
    if mutation == "source":
        (root / "src/support_agent/graph.py").write_text("changed")
    elif mutation == "new_file":
        (root / "tests/new.py").write_text("added")
    elif mutation == "config":
        monkeypatch.setitem(bench.CONFIG, "repeat", 4)
    else:
        manifest["files"] = {}
        (root / "data/benchmark_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="frozen_protocol"):
        bench.verify_manifest(root)


def test_freeze_cannot_overwrite_or_follow_paid_ledger(tmp_path):
    root = fixture_root(tmp_path)
    bench.freeze(root, root / ".local")
    with pytest.raises(ValueError, match="already exists"):
        bench.freeze(root, root / ".local")
    (root / ".local").mkdir()
    (root / ".local/budget.json").write_text("{}")
    with pytest.raises(ValueError, match="paid ledger"):
        bench.freeze(root, root / ".local")


def test_statistics_keep_failures_and_unknown_usage():
    rows = [
        {
            "case_id": "a",
            "repeat": 1,
            "experiment_variant": "B1",
            "status": "passed",
            "task_success": True,
            "input_tokens": 100,
            "latency_ms": 10,
        },
        {
            "case_id": "b",
            "repeat": 1,
            "experiment_variant": "B1",
            "status": "error",
            "task_success": False,
            "input_tokens": None,
            "failure_reason": "network",
            "latency_ms": 30,
        },
        {"case_id": "c", "repeat": 1, "experiment_variant": "B1", "status": "not_run", "task_success": False},
    ]
    result = bench.aggregate(rows)["B1"]
    assert result["planned_tasks"] == 3 and result["passed"] == 1
    assert result["task_success_rate"] == 1 / 3
    assert len(result["failures"]) == 2
    assert result["input_tokens"] is None and result["known_input_tokens"] == 100
    assert result["latency_ms"] == {"mean": 20, "median": 20, "p95": 30}


def test_pilot_rejects_incomplete_measurement_and_unsafe_results():
    rows = [
        {
            "experiment_variant": v,
            "task_success": True,
            "model_calls": 2,
            "model_response_count": 2,
            "input_tokens": 100,
            "output_tokens": 10,
            "unsafe_side_effects": 0,
            "resume_success": True,
        }
        for v in bench.CONFIG["variants"]
        for _ in range(bench.CONFIG["pilot_cases"])
    ]
    assert bench.pilot_valid(rows)
    for field, value in (
        ("model_response_count", 1),
        ("input_tokens", None),
        ("unsafe_side_effects", 1),
        ("protocol_errors", ["orphan"]),
        ("resume_success", False),
    ):
        broken = deepcopy(rows)
        broken[0][field] = value
        assert not bench.pilot_valid(broken)
