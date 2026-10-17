"""Offline tests for the paid smoke guard. Every HTTP response here is MockTransport."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("live_smoke", Path(__file__).parents[1] / "scripts/run_live_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def payload(**changes):
    return {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "disabled"},
        "stream": False,
        "max_tokens": 1024,
        **changes,
    }


def client_for(ledger, handler):
    return httpx.Client(transport=smoke.BoundedTransport(ledger, httpx.MockTransport(handler)))


def test_request_limit_and_persistent_ledger(tmp_path):
    ledger = smoke.BudgetLedger(tmp_path / "budget.json")
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

    with client_for(ledger, handle) as client:
        for _ in range(15):
            client.post("https://api.deepseek.com/chat/completions", json=payload())
        with pytest.raises(smoke.BudgetStopped):
            client.post("https://api.deepseek.com/chat/completions", json=payload())
    assert len(sent) == 15
    # A new process/object cannot reset previously reserved costs or counts.
    restarted = smoke.BudgetLedger(tmp_path / "budget.json")
    assert restarted.summary()["http_attempts"] == 15
    with pytest.raises(smoke.BudgetStopped):
        restarted.reserve(b"test")
    assert Decimal(restarted.summary()["cost_upper_usd"]) == Decimal("0.00063")


def test_unknown_usage_reserves_whole_context_before_send(tmp_path):
    ledger = smoke.BudgetLedger(tmp_path / "budget.json")
    sent = []

    def handle(request):
        state = json.loads(ledger.path.read_text())
        assert state["attempts"][-1]["status"] == "reserved"  # persisted BEFORE send
        sent.append(request)
        return httpx.Response(200, json={})

    with client_for(ledger, handle) as client:
        for _ in range(6):
            client.post("https://api.deepseek.com/chat/completions", json=payload())
        with pytest.raises(smoke.BudgetStopped):
            client.post("https://api.deepseek.com/chat/completions", json=payload())
    assert len(sent) == 6
    assert Decimal(ledger.summary()["cost_upper_usd"]) == Decimal("1.8948096") < 2


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "other"},
        {"max_tokens": 1025},
        {"stream": True},
        {"thinking": {"type": "enabled"}},
        {"n": 2},
        {"tools": [{"type": "web_search"}]},
    ],
)
def test_unapproved_configuration_never_sent(tmp_path, changes):
    ledger = smoke.BudgetLedger(tmp_path / "budget.json")

    def forbidden(request):
        pytest.fail("Unapproved request reached transport")

    with client_for(ledger, forbidden) as client, pytest.raises(smoke.BudgetStopped):
        client.post("https://api.deepseek.com/chat/completions", json=payload(**changes))
    assert ledger.summary()["http_attempts"] == 0


@pytest.mark.parametrize("kind", ["http_error", "timeout", "redirect"])
def test_api_error_halts_further_attempts_and_keeps_reservation(tmp_path, kind):
    ledger = smoke.BudgetLedger(tmp_path / "budget.json")

    def handle(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("mock timeout")
        return httpx.Response(
            401 if kind == "http_error" else 302, json={}, headers={"Location": "https://other.invalid"}
        )

    with client_for(ledger, handle) as client:
        if kind == "timeout":
            with pytest.raises(httpx.ReadTimeout):
                client.post("https://api.deepseek.com/chat/completions", json=payload())
        else:
            client.post("https://api.deepseek.com/chat/completions", json=payload())
        with pytest.raises(smoke.BudgetStopped):
            client.post("https://api.deepseek.com/chat/completions", json=payload())
    assert ledger.summary()["http_attempts"] == 1
    assert Decimal(ledger.summary()["cost_upper_usd"]) == smoke.RESERVATION


def test_crash_reservation_and_one_run_claim(tmp_path):
    ledger = smoke.BudgetLedger(tmp_path / "budget.json")
    ledger.claim_run()
    ledger.reserve(b"payload")  # crash before receiving any response
    restarted = smoke.BudgetLedger(ledger.path)
    with pytest.raises(smoke.BudgetStopped):
        restarted.claim_run()
    assert Decimal(restarted.summary()["cost_upper_usd"]) == smoke.RESERVATION


def test_usage_outside_contract_halts(tmp_path):
    ledger = smoke.BudgetLedger(tmp_path / "budget.json")
    index = ledger.reserve(b"payload")
    ledger.settle(index, http_status=200, usage={"prompt_tokens": 1, "completion_tokens": 1025, "total_tokens": 1026})
    assert ledger.summary()["stopped"] == "usage_contract_violation"
    with pytest.raises(smoke.BudgetStopped):
        ledger.reserve(b"again")


def test_budget_error_survives_sdk_wrapping(tmp_path, monkeypatch):
    from support_agent.evaluation import run_cases
    from support_agent.models import get_model

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-not-real")
    monkeypatch.setenv("DEEPSEEK_MAX_RETRIES", "0")
    ledger = smoke.BudgetLedger(tmp_path / "budget.json")
    # Make the first real transport attempt impossible, without making a network call.
    with ledger.edit() as state:
        state["stopped"] = "test"
    with client_for(ledger, lambda r: pytest.fail("budget bypass")) as client:
        report = run_cases(
            [json.loads(Path("data/eval_tasks.json").read_text())[2]],
            tmp_path,
            provider="deepseek",
            usage_source="mock",
            model_factory=lambda: get_model("deepseek", http_client=client),
        )
    assert report["results"][0]["failure_category"] == "budget_limit"
    assert report["results"][0]["model_error_count"] == 1
    assert ledger.summary()["http_attempts"] == 0


def test_offline_audit_never_claims_mock_as_real_or_rewrites_evidence(tmp_path, monkeypatch):
    from langchain_core.language_models import BaseChatModel

    from support_agent.evaluation import run_cases

    cases = json.loads(Path("data/eval_tasks.json").read_text())
    summary = run_cases([cases[0], cases[2], cases[3]], tmp_path)
    evidence = tmp_path / "live_result.json"
    evidence.write_text(
        json.dumps({"evaluation": summary, "budget": {"http_attempts": 0, "cost_upper_usd": "0", "stopped": None}})
    )
    before = evidence.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Offline audit must not call any model")

    monkeypatch.setattr(BaseChatModel, "invoke", forbidden)
    audit = smoke.audit_smoke(evidence)
    assert audit["new_api_calls"] == 0 and audit["gate"] == "FAIL"  # offline evidence cannot pass live Gate
    assert all(r["rejudged"]["task_success"] and not r["protocol_errors"] for r in audit["results"])
    assert evidence.read_bytes() == before
