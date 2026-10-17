"""Independent Stage 5 preflight probes. All HTTP uses local MockTransport."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest
from test_benchmark_budget import bench, ledger_at
from test_live_smoke import payload


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 0},
        {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
        {"prompt_tokens": 1.0, "completion_tokens": 1, "total_tokens": 2},
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3},
    ],
)
def test_review_unknown_or_invalid_wire_usage_preserves_full_reservation(tmp_path, usage):
    ledger = ledger_at(tmp_path)
    sent = []

    def handle(request):
        persisted = json.loads(ledger.path.read_text())
        assert persisted["attempts"][0]["status"] == "reserved"
        assert Decimal(persisted["attempts"][0]["upper_usd"]) == ledger.reservation
        sent.append(request)
        return httpx.Response(200, json={"usage": usage})

    with httpx.Client(transport=bench.BoundedTransport(ledger, httpx.MockTransport(handle))) as client:
        client.post("https://api.deepseek.com/chat/completions", json=payload())
    assert len(sent) == 1
    assert Decimal(ledger_at(tmp_path).summary()["cost_upper_usd"]) == ledger.reservation


def test_review_parallel_claim_only_one_process_object_can_start_phase(tmp_path):
    ledger_at(tmp_path)

    def claim(_):
        try:
            bench.claim_phase(ledger_at(tmp_path), "pilot", "frozen")
            return True
        except bench.BudgetStopped:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(12))) == 1
    assert ledger_at(tmp_path).summary()["http_attempts"] == 0


def test_review_error_keeps_own_and_inflight_reservations(tmp_path):
    ledger = ledger_at(tmp_path)
    ids = [ledger.reserve(str(i).encode()) for i in range(4)]
    ledger.settle(ids[0], http_status=503)
    ledger.settle(ids[1], http_status=200, usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
    state = ledger_at(tmp_path).summary()
    assert Decimal(state["cost_upper_usd"]) == ledger.reservation * 3 + Decimal("0.000042")
    assert state["http_attempts"] == 4 and state["stopped"] == "api_error"
    with pytest.raises(bench.BudgetStopped):
        ledger_at(tmp_path).reserve(b"after concurrent error")


def test_review_benchmark_passes_frozen_context_strategy(tmp_path, monkeypatch):
    from support_agent import evaluation

    captured = {}
    (tmp_path / "1-0.jsonl").write_text("")

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return {"run_dir": str(tmp_path), "results": [{"task_success": True}]}

    monkeypatch.setenv("SKILLMEM_CONTEXT_STRATEGY", "legacy_trim")
    monkeypatch.setattr(evaluation, "run_cases", fake_run)
    bench._execute_case(
        (0, {"case_id": "review", "category": "test"}, "M", 1),
        None,
        ledger_at(tmp_path),
        "pilot",
        {"protocol_hash": "fixed"},
    )
    assert captured.get("context_strategy") == "structured_memory"


@pytest.mark.parametrize("missing", ["resume_success", "resume_attempts", "service_restart_count", "task_success"])
def test_review_pilot_requires_real_approval_recovery_evidence(missing):
    rows = []
    for variant in bench.CONFIG["variants"]:
        for index in range(3):
            rows.append(
                {
                    "case_id": f"d_pilot_0{index + 1}",
                    "experiment_variant": variant,
                    "task_success": True,
                    "model_calls": 3,
                    "model_response_count": 3,
                    "input_tokens": 300,
                    "output_tokens": 30,
                    "unsafe_side_effects": 0,
                    "expected": {"requires_approval": index == 2},
                    "resume_success": True if index == 2 else None,
                    "resume_attempts": 1 if index == 2 else 0,
                    "service_restart_count": 1 if index == 2 else 0,
                }
            )
    assert bench.pilot_valid(rows)
    del rows[2][missing]
    assert not bench.pilot_valid(rows)


def test_review_concurrent_fixture_factories_do_not_leak(tmp_path):
    from support_agent import service as service_module
    from support_agent.evaluation import ToolFixture, fixture_tools
    from support_agent.policies_kb import PolicyBase
    from support_agent.policy import PolicyConfig
    from support_agent.service import default_data_dir
    from support_agent.store import Store

    original = service_module.make_tools
    a_entered, b_attempting, b_entered = (threading.Event() for _ in range(3))
    data = default_data_dir()

    def get_result():
        tools = service_module.make_tools(
            Store.load(data / "store.json"), PolicyBase(data / "policies.md"), PolicyConfig()
        )
        tool = next(t for t in tools if t.name == "get_order")
        return json.loads(tool.invoke({"order_id": "1005"}, {"configurable": {"customer_id": "C100"}}))

    def padded():
        with fixture_tools(ToolFixture(padding_tool="get_order", padding_chars=19), {}):
            a_entered.set()
            assert b_attempting.wait(2)
            b_entered.wait(0.1)  # A correct lock holds B outside until A leaves.
            return get_result()

    def plain():
        assert a_entered.wait(2)
        b_attempting.set()
        with fixture_tools(ToolFixture(), {}):
            b_entered.set()
            return get_result()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(padded), pool.submit(plain)
            padded_result, plain_result = a.result(timeout=5), b.result(timeout=5)
        assert padded_result["_fixture_padding"] == "x" * 19
        assert "_fixture_padding" not in plain_result
        assert service_module.make_tools is original
    finally:
        service_module.make_tools = original
