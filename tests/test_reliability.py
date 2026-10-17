"""B1 integration/fault injection: all business fixtures are synthetic, all models offline."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from time import sleep

import pytest
from conftest import ScriptedToolModel, call
from langchain_core.messages import AIMessage, ToolMessage

from support_agent.evaluation import run_cases, snapshot
from support_agent.execution import ExecutionLimits
from support_agent.models import get_model
from support_agent.service import ConversationError, SupportService
from support_agent.tracing import TraceRecorder


@pytest.fixture
def b1(tmp_path):
    services = []

    def create(*replies, **kwargs):
        kwargs.setdefault("db_path", str(tmp_path / f"service-{len(services)}.sqlite"))
        if replies:
            kwargs["model"] = ScriptedToolModel(replies=list(replies))
        service = SupportService(variant="B1", **kwargs)
        services.append(service)
        return service

    yield create
    for service in services:
        service.close()


def messages(service, thread="t"):
    return service.graph.get_state(service._config("C100", thread)).values["messages"]


def refunds(service):
    service.store.refresh()
    return service.store.refunds


def test_repeated_refund_new_tool_ids_returns_same_receipt(b1, tmp_path):
    trace = TraceRecorder(tmp_path / "duplicate.jsonl", run_id="run", thread_id="t", source="mock")
    refund = call("request_refund", order_id="1005", amount=18, reason="broken")
    service = b1(refund, refund, AIMessage("Refunded 18.00 EUR"), trace=trace)
    result = service.chat("C100", "Refund", "t")
    observations = [json.loads(m.content) for m in messages(service) if isinstance(m, ToolMessage)]
    assert result.status == "answered" and len(refunds(service)) == 1
    assert observations[0]["refund_id"] == observations[1]["refund_id"]
    assert observations[1]["idempotent_replay"] is True
    stats = trace.statistics()
    assert stats["tool_proposed_count"] == stats["tool_executed_count"] == 2
    assert stats["tool_idempotent_replay_count"] == 1
    trace.close()


@pytest.mark.parametrize("amount", [17, 19])
def test_changed_amount_cannot_reuse_successful_operation(b1, amount):
    service = b1(
        call("request_refund", order_id="1005", amount=18, reason="broken"),
        call("request_refund", order_id="1005", amount=amount, reason="again"),
        AIMessage("done"),
    )
    service.chat("C100", "refund", "t")
    last = [m for m in messages(service) if isinstance(m, ToolMessage)][-1]
    assert "idempotency_conflict" in last.content
    assert len(refunds(service)) == 1 and refunds(service)[0].amount == Decimal("18")


@pytest.mark.parametrize("approved", [True, False])
def test_approval_decision_idempotent_and_conflicting_decision_rejected(b1, approved):
    service = b1()
    waiting = service.chat("C100", "Refund order 1001, damaged", "t")
    assert waiting.status == "awaiting_approval" and not refunds(service)
    first = service.resolve_approval("t", approved, "reviewer", "fixture decision")
    counts = service.control.counts("t")
    assert service.resolve_approval("t", approved, "reviewer", "fixture decision") == first
    assert service.control.counts("t") == counts
    with pytest.raises(ConversationError) as exc:
        service.resolve_approval("t", not approved, "reviewer", "fixture decision")
    assert exc.value.status_code == 409
    assert len(refunds(service)) == int(approved)
    assert not service.pending_approvals()
    assert service.registry.approval("t")["state"] == "resolved"


@pytest.mark.parametrize("when", ["before", "after"])
def test_resume_failure_preserves_decision_and_can_recover(b1, monkeypatch, when):
    service = b1()
    service.chat("C100", "Refund order 1001, damaged", "t")
    original = service.graph.invoke

    def fail(*args, **kwargs):
        if when == "after":
            original(*args, **kwargs)
        raise RuntimeError("injected resume failure")

    monkeypatch.setattr(service.graph, "invoke", fail)
    with pytest.raises(RuntimeError, match="injected"):
        service.resolve_approval("t", True, "reviewer")
    assert service.pending_approvals()[0]["state"] == "retryable"
    assert service.registry.approval("t")["decision"]["approved"] is True
    assert len(refunds(service)) == int(when == "after")
    monkeypatch.setattr(service.graph, "invoke", original)
    result = service.resolve_approval("t", True, "reviewer")
    assert result.status == "answered" and len(refunds(service)) == 1
    assert not service.pending_approvals()
    ids = [m.tool_call_id for m in messages(service) if isinstance(m, ToolMessage)]
    calls = [c["id"] for m in messages(service) if isinstance(m, AIMessage) for c in m.tool_calls]
    assert sorted(ids) == sorted(calls)


def test_lost_pending_write_reconstructed_from_checkpoint(b1, monkeypatch):
    service = b1()
    original = service.registry.journal_pending
    monkeypatch.setattr(service.registry, "journal_pending", lambda *args: (_ for _ in ()).throw(OSError("disk fault")))
    with pytest.raises(OSError):
        service.chat("C100", "Refund order 1001, damaged", "t")
    assert not refunds(service)
    monkeypatch.setattr(service.registry, "journal_pending", original)
    assert service.pending_approvals()[0]["order_id"] == "1001"
    assert service.resolve_approval("t", True, "reviewer").status == "answered"
    assert len(refunds(service)) == 1


@pytest.mark.parametrize("change", ["cancelled", "amount", "owner", "expired"])
def test_approval_revalidates_changed_business_state(b1, change):
    service = b1()
    service.chat("C100", "Refund order 1001, damaged", "t")
    with service.store.transaction():
        order = service.store.orders["1001"]
        if change == "cancelled":
            order.status = "cancelled"
        elif change == "amount":
            order.total = Decimal("10")
        elif change == "owner":
            order.customer_id = "C200"
        else:
            from datetime import date

            service.store.today = date(2026, 1, 1)
    service.resolve_approval("t", True, "reviewer")
    assert not refunds(service) and not service.pending_approvals()
    observations = [json.loads(m.content) for m in messages(service) if isinstance(m, ToolMessage)]
    assert observations[-1]["status"] == "rejected"


@pytest.mark.parametrize(
    "bad", [call("missing"), call("request_refund"), call("get_order", order_id="1005", customer_id="C200")]
)
def test_illegal_calls_have_paired_errors_without_execution(b1, bad, tmp_path):
    trace = TraceRecorder(tmp_path / "bad.jsonl", run_id="r", thread_id="t", source="mock")
    service = b1(bad, AIMessage("cannot comply"), trace=trace)
    before = snapshot(service.store)
    service.chat("C100", "request", "t")
    state = messages(service)
    proposed = next(m for m in state if isinstance(m, AIMessage)).tool_calls[0]
    observation = next(m for m in state if isinstance(m, ToolMessage))
    assert observation.tool_call_id == proposed["id"] and observation.status == "error"
    assert json.loads(observation.content)["retryable"] is False
    assert snapshot(service.store) == before
    assert trace.statistics()["tool_executed_count"] == 0
    trace.close()


def test_multiple_tools_all_rejected_with_complete_protocol(b1):
    batch = AIMessage(
        "",
        tool_calls=[
            *call("request_refund", order_id="1005", amount=18, reason="x").tool_calls,
            *call("cancel_order", order_id="1004").tool_calls,
        ],
    )
    service = b1(batch, AIMessage("cannot do both"))
    before = snapshot(service.store)
    service.chat("C100", "both", "t")
    state = messages(service)
    proposed = next(m for m in state if isinstance(m, AIMessage)).tool_calls
    observations = [m for m in state if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in observations} == {c["id"] for c in proposed}
    assert len(observations) == 2 and all(m.status == "error" for m in observations)
    assert all("multiple_tool_calls_not_allowed" in m.content for m in observations)
    assert snapshot(service.store) == before
    assert service.control.counts("t") == {"model_calls": 2, "tool_calls": 0}


@pytest.mark.parametrize(
    "limit,counts,reason",
    [
        (ExecutionLimits(model_calls=2), (2, 2), "model_calls_exhausted"),
        (ExecutionLimits(tool_calls=1), (2, 1), "tool_calls_exhausted"),
    ],
)
def test_execution_budget_ends_without_recursion_fallback(b1, limit, counts, reason):
    service = b1(call("get_order", order_id="1005"), limits=limit)
    result = service.chat("C100", "loop", "t")
    assert result.status == "failed" and reason in result.reply
    assert tuple(service.control.counts("t").values()) == counts
    assert not refunds(service) and not service.store.tickets
    assert not service.graph.get_state(service._config("C100", "t")).next


def test_tool_timeout_rolls_back_and_terminates_without_retry(b1, monkeypatch):
    service = b1(
        call("request_refund", order_id="1005", amount=18, reason="broken"), limits=ExecutionLimits(tool_seconds=0.01)
    )
    original = service.store.remember

    def slow(*args):
        original(*args)
        sleep(0.025)

    monkeypatch.setattr(service.store, "remember", slow)
    result = service.chat("C100", "refund", "t")
    assert result.status == "failed" and "tool_timeout" in result.reply
    assert not refunds(service)
    assert service.store._db.execute("SELECT COUNT(*) FROM business_operations").fetchone()[0] == 0
    assert service.control.counts("t") == {"model_calls": 1, "tool_calls": 1}
    assert [m for m in messages(service) if isinstance(m, ToolMessage)][-1].status == "error"


def test_model_error_after_refund_does_not_repeat_mutation(b1, monkeypatch):
    model = get_model("offline")
    original = type(model)._generate

    def fail_on_refund(self, messages, **kwargs):
        if any(isinstance(m, ToolMessage) and '"status": "refunded"' in m.content for m in messages):
            raise TimeoutError("injected model timeout")
        return original(self, messages, **kwargs)

    monkeypatch.setattr(type(model), "_generate", fail_on_refund)
    service = b1(model=model)
    result = service.chat("C100", "Refund order 1005", "t")
    assert result.status == "failed" and "model_timeout" in result.reply
    assert len(refunds(service)) == 1
    counts = service.control.counts("t")
    assert service.retry_turn("C100", "t").status == "failed"
    assert service.control.counts("t") == counts and len(refunds(service)) == 1


def test_same_thread_calls_are_serialized_across_service_instances(b1, monkeypatch, tmp_path):
    db = str(tmp_path / "shared.sqlite")
    one, two = b1(db_path=db), b1(db_path=db)
    first_started, second_attempted, release = threading.Event(), threading.Event(), threading.Event()
    model_type = type(get_model("offline"))
    original = model_type._generate
    active, peak = 0, 0
    lock = threading.Lock()

    def wait(self, messages, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        first_started.set()
        assert release.wait(3)
        try:
            return original(self, messages, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(model_type, "_generate", wait)

    def second():
        second_attempted.set()
        return two.chat("C100", "Where is order 1002?", "shared")

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(one.chat, "C100", "Where is order 1002?", "shared")
        assert first_started.wait(3)
        b = pool.submit(second)
        assert second_attempted.wait(3)
        sleep(0.03)
        assert not b.done()
        release.set()
        assert a.result().status == b.result().status == "answered"
    assert peak == 1
    assert [h["role"] for h in one.history("C100", "shared")] == ["customer", "assistant"] * 2


def test_b0_b1_normal_business_oracles_and_usage_match(tmp_path):
    cases = json.loads(Path("data/eval_tasks.json").read_text())
    reports = [run_cases(cases, tmp_path / variant, variant=variant, repeat=2) for variant in ["B0-compatible", "B1"]]
    assert all(r["passed"] == r["tasks"] == 14 for r in reports)
    for left, right in zip(reports[0]["results"], reports[1]["results"], strict=True):
        for field in ["task_success", "sop_compliance", "tool_proposed_count", "tool_executed_count", "input_tokens"]:
            assert left[field] == right[field]
        assert right["execution_limits"] == {"model_calls": 8, "tool_calls": 8, "turn_seconds": 120, "tool_seconds": 5}
    assert not list(tmp_path.rglob("*.sqlite"))


def test_original_scenarios_b1():
    from support_agent.evaluate import run_scenarios

    results = run_scenarios("offline", variant="B1")
    assert len(results) == 11 and all(r["passed"] for r in results)


def test_original_evaluator_failure_has_nonzero_exit(monkeypatch):
    from support_agent.evaluate import main

    monkeypatch.setattr("sys.argv", ["evaluate"])
    monkeypatch.setattr(
        "support_agent.evaluate.run_scenarios",
        lambda *a, **kw: [dict(name="fault", passed=False, failed_checks=["reply"], status="failed", tools=[])],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def test_old_decision_cannot_resolve_a_later_approval(b1):
    service = b1()
    first = service.chat("C100", "Refund order 1001, damaged", "t")
    first_id = first.approval["approval_id"]
    denied = service.resolve_approval("t", False, "reviewer", approval_id=first_id)
    second = service.chat("C100", "Refund order 1001, damaged", "t")
    second_id = second.approval["approval_id"]
    assert first_id != second_id
    assert service.resolve_approval("t", False, "reviewer", approval_id=first_id) == denied
    assert not refunds(service) and service.pending_approvals()[0]["approval_id"] == second_id
    with pytest.raises(ConversationError, match="approval_id"):
        service.resolve_approval("t", True, "reviewer")
    assert service.resolve_approval("t", True, "reviewer", approval_id=second_id).status == "answered"
    assert len(refunds(service)) == 1


@pytest.mark.parametrize("question", ["退款政策", "退货期限", "取消订单", "物流配送", "保修规则"])
def test_b1_policy_aliases_are_small_keyword_mapping(b1, question):
    service = b1(call("search_policies", query=question), AIMessage("policy"))
    service.chat("C100", question, "t")
    result = next(m for m in messages(service) if isinstance(m, ToolMessage))
    assert json.loads(result.content)["results"]


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, 0])
def test_nonfinite_and_nonpositive_refund_rejected(b1, amount):
    service = b1(call("request_refund", order_id="1005", amount=amount, reason="bad"), AIMessage("no"))
    service.chat("C100", "refund", "t")
    assert not refunds(service)
    result = next(m for m in messages(service) if isinstance(m, ToolMessage))
    assert json.loads(result.content)["status"] == "rejected"


@pytest.mark.parametrize(
    "test_name",
    [
        "test_mock_multistep_tool_ids_and_usage",
        "test_mock_approval_resume_retains_pending_call_id",
        "test_malformed_arguments_observed_without_execution",
    ],
)
def test_deepseek_protocol_suite_on_b1(test_name, tmp_path, monkeypatch):
    import test_deepseek

    monkeypatch.setenv("SKILLMEM_VARIANT", "B1")
    getattr(test_deepseek, test_name)(tmp_path=tmp_path, monkeypatch=monkeypatch)


def test_approval_budget_is_not_reset_by_resume(b1):
    service = b1(limits=ExecutionLimits(tool_calls=2))
    waiting = service.chat("C100", "Refund order 1001, damaged", "t")
    assert waiting.status == "awaiting_approval"
    assert service.control.counts("t")["tool_calls"] == 2
    result = service.resolve_approval("t", True, "reviewer")
    assert result.status == "failed" and "tool_calls_exhausted" in result.reply
    assert not refunds(service) and not service.pending_approvals()
    assert service.control.counts("t")["tool_calls"] == 2


def test_b1_api_failed_model_is_explicit_not_answered(b1, monkeypatch):
    from fastapi.testclient import TestClient

    from support_agent.api import create_app

    model = get_model("offline")

    def fail(*args, **kwargs):
        raise RuntimeError("injected model failure")

    monkeypatch.setattr(type(model), "_generate", fail)
    service = b1(model=model)
    with TestClient(create_app(service=service)) as client:
        result = client.post("/chat", headers={"X-Customer-Id": "C100"}, json={"message": "refund"})
    assert result.status_code == 200 and result.json()["status"] == "failed"
    assert not refunds(service)


def test_budget_failure_runner_reason_and_remaining_cases(tmp_path):
    cases = json.loads(Path("data/eval_tasks.json").read_text())
    models = iter([ScriptedToolModel(replies=[call("get_order", order_id="1005")]), get_model("offline")])
    summary = run_cases(
        [cases[1], cases[0]],
        tmp_path,
        variant="B1",
        limits=ExecutionLimits(model_calls=2),
        model_factory=lambda: next(models),
    )
    assert summary["results"][0]["termination_reason"] == "model_calls_exhausted"
    assert summary["results"][0]["failure_category"] == "budget_limit"
    assert summary["results"][1]["task_success"]


def test_reused_tool_call_id_retains_both_observations(b1, tmp_path, monkeypatch):
    from test_deepseek import mock_model, response

    wire_call = [("same-call-id", "request_refund", {"order_id": "1005", "amount": 18, "reason": "broken"})]
    model, requests, client = mock_model(
        monkeypatch, [response(calls=wire_call), response(calls=wire_call), response("done")]
    )
    trace = TraceRecorder(tmp_path / "replay.jsonl", run_id="r", thread_id="t", source="mock")
    try:
        service = b1(model=model, trace=trace)
        service.chat("C100", "refund", "t")
        assert len(refunds(service)) == 1
        observations = [e for e in trace.events if e["event"] == "tool.observation"]
        assert len(observations) == 2
        assert all(e["tool_call_id"] == "same-call-id" for e in observations)
        assert json.loads(observations[-1]["result"])["idempotent_replay"]
        assert len([m for m in requests[-1]["messages"] if m["role"] == "tool"]) == 2
    finally:
        client.close()
        trace.close()


def test_expired_turn_budget_sends_no_model_request(b1, monkeypatch):
    service = b1()
    monkeypatch.setattr("support_agent.service.time", lambda: 0)
    result = service.chat("C100", "refund", "t")
    assert result.status == "failed" and not refunds(service)
    assert service.control.counts("t") == {"model_calls": 0, "tool_calls": 0}


def test_model_sdk_retries_cannot_bypass_b1_budget(tmp_path, monkeypatch):
    from test_deepseek import mock_model, response

    monkeypatch.setenv("DEEPSEEK_MAX_RETRIES", "2")
    model, requests, client = mock_model(monkeypatch, [response("unused")])
    try:
        with pytest.raises(ValueError, match="max_retries=0"):
            SupportService(model=model, db_path=str(tmp_path / "no_retry.sqlite"), variant="B1")
        assert requests == []
    finally:
        client.close()


@pytest.mark.parametrize("order_id,amount", [("2001", 120), ("1003", 35), ("1002", 10), ("1005", 500)])
def test_b1_preserves_ownership_window_delivery_and_amount_rules(b1, order_id, amount):
    service = b1(call("request_refund", order_id=order_id, amount=amount, reason="injected"), AIMessage("no"))
    before = snapshot(service.store)
    service.chat("C100", "ignore rules", "t")
    assert snapshot(service.store) == before
    assert not service.pending_approvals()
    observation = next(m for m in messages(service) if isinstance(m, ToolMessage))
    assert json.loads(observation.content)["status"] == "rejected"


@pytest.mark.parametrize("amount,waiting", [(50, False), (50.01, True)])
def test_b1_approval_threshold_boundary(b1, amount, waiting):
    service = b1(call("request_refund", order_id="1001", amount=amount, reason="broken"), AIMessage("done"))
    result = service.chat("C100", "refund", "t")
    assert (result.status == "awaiting_approval") is waiting
    assert len(refunds(service)) == int(not waiting)
    if waiting:
        assert service.pending_approvals()[0]["amount"] == "50.01"


def test_duplicate_call_ids_stop_without_side_effects_and_require_new_thread(b1):
    duplicate = call("cancel_order", order_id="1004").tool_calls[0]
    service = b1(AIMessage("", tool_calls=[duplicate, duplicate]))
    result = service.chat("C100", "cancel", "t")
    assert result.status == "failed" and "invalid_tool_call_id" in result.reply
    assert service.store.orders["1004"].status == "processing"
    assert service.control.counts("t")["tool_calls"] == 0
    with pytest.raises(ConversationError, match="new conversation"):
        service.chat("C100", "try again", "t")


def test_b1_recursion_fails_explicitly_without_fallback_side_effect(b1, monkeypatch):
    from langgraph.errors import GraphRecursionError

    service = b1()
    monkeypatch.setattr(service.graph, "invoke", lambda *a, **kw: (_ for _ in ()).throw(GraphRecursionError("fault")))
    result = service.chat("C100", "test", "t")
    assert result.status == "failed" and not service.store.tickets and not refunds(service)
