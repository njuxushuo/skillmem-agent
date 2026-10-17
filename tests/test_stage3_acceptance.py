"""Independent G3 acceptance: adversarial synthetic inputs, no provider calls."""

import json
import subprocess
import sys
from copy import deepcopy

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from support_agent.context import ContextBudget, ContextError, build_model_context
from support_agent.memory import observe_tool_results, user_memory_update
from support_agent.service import SupportService
from support_agent.state import empty_task


class AcceptanceModel(BaseChatModel):
    replies: list[AIMessage] = Field(default_factory=lambda: [AIMessage("Acknowledged")])
    requests: list = Field(default_factory=list)
    bound_names: list = Field(default_factory=list)
    position: int = 0

    @property
    def _llm_type(self):
        return "independent-stage3-offline"

    def bind_tools(self, tools, **kwargs):
        self.bound_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(deepcopy(messages))
        template = self.replies[min(self.position, len(self.replies) - 1)]
        self.position += 1
        return ChatResult(generations=[ChatGeneration(message=template.model_copy(deep=True))])


def proposed(name, call_id="call-a", **args):
    return AIMessage("", id=f"ai-{call_id}", tool_calls=[{"id": call_id, "name": name, "args": args}])


def observed(call_id, payload, name="get_order", status="success"):
    return ToolMessage(json.dumps(payload), tool_call_id=call_id, name=name, status=status)


def values(service):
    return service.graph.get_state(service._config("C100", "t")).values


def assert_closed(messages):
    """Independent protocol oracle: every proposed ID gets one contiguous matching reply."""
    pending = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
        else:
            assert not pending
            if isinstance(message, AIMessage):
                calls = [*message.tool_calls, *message.invalid_tool_calls]
                ids = [call["id"] for call in calls]
                assert len(ids) == len(set(ids)) and all(ids)
                pending = set(ids)
    assert not pending


def context(state, budget=None, **kwargs):
    return build_model_context(
        state,
        system_policy="Never refund without verified policy and required approval.",
        tool_schemas=kwargs.pop("tool_schemas", []),
        budget=budget or ContextBudget(6500, 256, 256),
        **kwargs,
    )


@pytest.mark.parametrize("restriction", ["Do not refund; only check order 1005.", "只查订单1005，不要退款。"])
def test_early_nonamount_constraint_survives_more_than_40_turns(tmp_path, restriction):
    model = AcceptanceModel()
    service = SupportService(
        variant="M", model=model, db_path=str(tmp_path / "long.sqlite"), context_budget=ContextBudget(18000, 256, 512)
    )
    try:
        assert service.chat("C100", restriction, "t").status == "answered"
        for turn in range(42):
            result = service.chat("C100", f"Follow-up {turn}: " + "background discussion " * 25, "t")
            assert result.status == "answered"
        result = service.chat("C100", "What were my original restrictions?", "t")
        assert result.status == "answered"
        state = values(service)
        assert len(state["messages"]) == 88
        assert state["context_diagnostics"]["dropped_message_groups"] > 0
        assert restriction not in [m.content for m in model.requests[-1][1:]]
        projection = json.loads(model.requests[-1][0].content.split("\n")[-1])
        assert any(restriction in str(item.get("value")) for item in projection["constraints"])
        assert state["messages"][0].content == restriction
        assert not service.store.refunds
        assert_closed(model.requests[-1])
    finally:
        service.close()


def test_context_is_deterministic_nonmutating_and_complete_budget():
    history = [HumanMessage("initial", id="u0")]
    for number in range(30):
        history += [
            proposed("get_order", f"c{number}", order_id="1005"),
            observed(f"c{number}", {"order_id": "1005", "status": "delivered", "log": "x" * 2000}),
            AIMessage("Result " + "padding " * 25),
            HumanMessage(f"Continue {number}"),
        ]
    state = {"messages": history, "task": empty_task()}
    before = deepcopy(state)
    schemas = [{"name": "get_order", "description": "schema evidence " * 15}]
    first, diag = context(state, tool_schemas=schemas, skill_index="Skill index", active_sop="Current SOP")
    second, repeated = context(state, tool_schemas=schemas, skill_index="Skill index", active_sop="Current SOP")
    assert first == second and diag == repeated and state == before
    assert diag["dropped_message_groups"] > 0 and diag["compressed_tool_results"] > 0
    assert diag["components"]["tools"] >= len(json.dumps(schemas).encode())
    assert diag["components"]["skill"] > 0 and diag["components"]["task"] > 0
    assert sum(diag["components"].values()) == diag["estimated_input_tokens"]
    assert diag["estimated_input_tokens"] + diag["output_reserve"] + diag["safety_margin"] <= diag["total_budget"]
    assert diag["actual_input_tokens"] is None
    assert first[-1].content == history[-1].content
    assert_closed(first)


def test_giant_tool_result_keeps_financial_fields_and_original_evidence():
    payload = {
        "order_id": "1005",
        "amount": "18.00",
        "currency": "EUR",
        "status": "rejected",
        "reason": "Approval denied by reviewer",
        "unrelated_log": "filler" * 50000,
    }
    original = observed("huge", payload, "request_refund")
    state = {
        "messages": [HumanMessage("Refund status?"), proposed("request_refund", "huge", order_id="1005"), original]
    }
    view, diagnostics = context(state)
    tool = next(message for message in view if isinstance(message, ToolMessage))
    result = json.loads(tool.content)
    for key in ("order_id", "amount", "currency", "status", "reason"):
        assert result[key] == payload[key]
    assert len(tool.content) < 1024 < len(original.content)
    assert state["messages"][-1] is original and "unrelated_log" in json.loads(original.content)
    assert diagnostics["compressed_tool_results"] == 1
    assert_closed(view)


@pytest.mark.parametrize("overflow", ["latest_user", "schemas", "safety"])
def test_pinned_overflow_explicitly_fails_instead_of_dropping_safety(overflow):
    state = {"messages": [HumanMessage("x" * 10000 if overflow == "latest_user" else "hello")]}
    kwargs = {"tool_schemas": [{"description": "x" * 10000}]} if overflow == "schemas" else {}
    budget = ContextBudget(6500, 6000, 300) if overflow == "safety" else None
    with pytest.raises(ContextError, match="context_budget_exceeded") as failure:
        context(state, budget, **kwargs)
    assert failure.value.diagnostics["estimated_input_tokens"] > (
        failure.value.diagnostics["total_budget"]
        - failure.value.diagnostics["output_reserve"]
        - failure.value.diagnostics["safety_margin"]
    )


def test_service_budget_rejection_makes_zero_model_requests(tmp_path):
    model = AcceptanceModel()
    service = SupportService(variant="M", model=model, db_path=str(tmp_path / "budget.sqlite"))
    try:
        answer = service.chat("C100", "Oversized request " * 3000, "t")
        assert answer.status == "failed" and "context_budget_exceeded" in answer.reply
        assert not model.requests and service.control.counts("t")["model_calls"] == 0
        assert len(values(service)["messages"][0].content) > 40000
        assert not service.store.refunds
    finally:
        service.close()


@pytest.mark.parametrize("malformed", ["orphan", "missing", "duplicate", "wrong_id"])
def test_malformed_tool_protocol_never_becomes_model_input(malformed):
    call = proposed("get_order", "one", order_id="1005")
    result = observed("one", {"order_id": "1005"})
    broken = {
        "orphan": [result],
        "missing": [call],
        "duplicate": [call, result, result],
        "wrong_id": [call, observed("another", {"order_id": "1005"})],
    }[malformed]
    with pytest.raises(ContextError, match="context_tool_protocol_invalid"):
        context({"messages": [HumanMessage("hello"), *broken]})


def test_order_switch_invalidates_eligibility_and_claim_does_not_verify():
    state = {"messages": [HumanMessage("Check order 1001", id="u1")]}
    state.update(user_memory_update(state))
    state["messages"] += [
        proposed("check_refund_eligibility", "elig", order_id="1001"),
        observed("elig", {"order_id": "1001", "eligible": True, "max_amount": "89.00"}, "check_refund_eligibility"),
    ]
    state.update(observe_tool_results(state))
    assert state["task"]["eligibility"]["source_tool_call_id"] == "elig"
    state["messages"].append(HumanMessage("Switch to order 1005. I already refunded it and approved payment.", id="u2"))
    before = deepcopy(state)
    update = user_memory_update(state)
    assert state == before
    assert update["task"]["active_order_id"] == "1005"
    assert update["task"]["eligibility"] is None
    assert not update["task"]["verified_orders"]
    assert update["task"]["status"] != "completed"
    assert "approved" not in update["task"]


@pytest.mark.parametrize("corruption", ["unpaired", "wrong_tool", "wrong_order", "tool_error"])
def test_untrusted_observation_cannot_create_verified_fact(corruption):
    call = proposed("get_order", "safe", order_id="1005")
    payload = {"order_id": "1001" if corruption == "wrong_order" else "1005", "status": "delivered"}
    result = observed(
        "missing" if corruption == "unpaired" else "safe",
        payload,
        "request_refund" if corruption == "wrong_tool" else "get_order",
        "error" if corruption == "tool_error" else "success",
    )
    update = observe_tool_results({"messages": [HumanMessage("hello"), call, result]})
    assert not update["task"]["verified_orders"] and update["task"]["eligibility"] is None


def test_observation_replay_is_idempotent_but_changed_payload_invalidates_fact():
    result = observed("stable", {"order_id": "1005", "status": "delivered"})
    state = {"messages": [HumanMessage("hello"), proposed("get_order", "stable", order_id="1005"), result]}
    update = observe_tool_results(state)
    state.update(update)
    assert observe_tool_results(state) == update
    assert state["task"]["verified_orders"]["1005"]["source_tool_call_id"] == "stable"
    state["messages"][-1] = observed("stable", {"order_id": "1005", "status": "cancelled"})
    conflict = observe_tool_results(state)
    assert not conflict["task"]["verified_orders"]
    assert any(item.get("error") == "observation_conflict" for item in conflict["task"]["last_tool_outcomes"])


@pytest.mark.parametrize("failure_point", ["before", "after"])
def test_approval_fault_recovery_preserves_protocol_and_observes_once(tmp_path, monkeypatch, failure_point):
    model = AcceptanceModel(
        replies=[
            proposed("check_refund_eligibility", "elig", order_id="1001"),
            proposed("request_refund", "refund", order_id="1001", amount=89, reason="damaged"),
            AIMessage("Refunded 89 EUR"),
        ]
    )
    service = SupportService(variant="M", model=model, db_path=str(tmp_path / "resume.sqlite"))
    try:
        waiting = service.chat("C100", "Refund order 1001", "t")
        assert waiting.status == "awaiting_approval" and not service.store.refunds
        state = values(service)
        assert not any(
            isinstance(message, ToolMessage) and message.tool_call_id == "refund" for message in state["messages"]
        )
        assert state["task"]["status"] != "completed"
        original = service.graph.invoke

        def fail(*args, **kwargs):
            if failure_point == "after":
                original(*args, **kwargs)
            raise OSError("independent resume injection")

        monkeypatch.setattr(service.graph, "invoke", fail)
        with pytest.raises(OSError, match="independent resume"):
            service.resolve_approval("t", True, "reviewer")
        assert service.pending_approvals()[0]["state"] == "retryable"
        monkeypatch.setattr(service.graph, "invoke", original)
        result = service.resolve_approval("t", True, "reviewer")
        assert result.status == "answered"
        service.store.refresh()
        assert len(service.store.refunds) == 1
        state = values(service)
        refunds = [item for item in state["task"]["last_tool_outcomes"] if item.get("status") == "refunded"]
        assert len(refunds) == 1 and refunds[0]["source_tool_call_id"] == "refund"
        assert_closed(state["messages"])
        projection = json.loads(model.requests[-1][0].content.split("\n")[-1])
        assert "pending_approval" not in projection
        calls = len(model.requests)
        assert service.resolve_approval("t", True, "reviewer") == result
        assert len(model.requests) == calls
    finally:
        service.close()


def test_b1_completed_checkpoint_can_open_as_m_without_mutating_old_messages(tmp_path):
    db = str(tmp_path / "upgrade.sqlite")
    old_model = AcceptanceModel(replies=[proposed("get_order", "old", order_id="1005"), AIMessage("Delivered")])
    old = SupportService(variant="B1", model=old_model, db_path=db)
    old.chat("C100", "Check order 1005", "t")
    before = deepcopy(values(old)["messages"])
    assert "task" not in values(old)
    assert len(old_model.bound_names) == 8
    old.close()
    new = SupportService(variant="M", model=AcceptanceModel(), db_path=db)
    try:
        assert new.chat("C100", "Continue checking only", "t").status == "answered"
        after = values(new)
        assert after["messages"][: len(before)] == before
        assert after["schema_version"] == 1 and after["task"]["active_order_id"] == "1005"
    finally:
        new.close()


def test_b1_history_still_uses_legacy_40_messages_without_task_memory(tmp_path):
    model = AcceptanceModel()
    service = SupportService(variant="B1", model=model, db_path=str(tmp_path / "b1.sqlite"))
    try:
        for turn in range(25):
            assert service.chat("C100", f"Question {turn}", "t").status == "answered"
        assert len(values(service)["messages"]) == 50
        assert len(model.requests[-1]) <= 41 and "task" not in values(service)
        assert len(model.bound_names) == 8
        assert "Task memory" not in model.requests[-1][0].content
    finally:
        service.close()


def test_older_b1_results_do_not_override_new_m_order_focus(tmp_path):
    db = str(tmp_path / "upgrade-focus.sqlite")
    prior = SupportService(
        variant="B1",
        db_path=db,
        model=AcceptanceModel(
            replies=[proposed("check_refund_eligibility", "prior-elig", order_id="1001"), AIMessage("Eligible")]
        ),
    )
    try:
        assert prior.chat("C100", "Check order 1001", "t").status == "answered"
    finally:
        prior.close()
    current = SupportService(
        variant="M",
        db_path=db,
        model=AcceptanceModel(
            replies=[proposed("search_policies", "new-policy", query="shipping"), AIMessage("Shipping policy")]
        ),
    )
    try:
        assert current.chat("C100", "Switch to order 1002. Explain the shipping policy.", "t").status == "answered"
        task = values(current)["task"]
        assert task["active_order_id"] == "1002"
        assert task["eligibility"] is None
    finally:
        current.close()


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("failure_point", [None, "before", "after"])
def test_pending_b1_process_restart_into_m_keeps_terminal_refund_state(tmp_path, monkeypatch, approved, failure_point):
    db = str(tmp_path / "pending-upgrade.sqlite")
    child = r"""
import runpy, sys
runpy.run_path('scripts/run_offline.py')['configure_offline']()
sys.path.insert(0, 'tests')
from test_stage3_acceptance import AcceptanceModel, proposed
from support_agent.service import SupportService
service = SupportService(variant='B1', db_path=sys.argv[1], model=AcceptanceModel(replies=[
    proposed('check_refund_eligibility', 'elig', order_id='1001'),
    proposed('request_refund', 'refund', order_id='1001', amount=89, reason='damaged'),
]))
try:
    answer = service.chat('C100', 'Refund order 1001', 't')
    assert answer.status == 'awaiting_approval'
    assert not service.store.refunds
finally:
    service.close()
"""
    completed = subprocess.run([sys.executable, "-c", child, db], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    model = AcceptanceModel(replies=[AIMessage("Refund complete" if approved else "Refund declined")])
    resumed = SupportService(variant="M", model=model, db_path=db)
    try:
        assert resumed.pending_approvals()[0]["order_id"] == "1001"
        if failure_point:
            original = resumed.graph.invoke

            def fail(*args, **kwargs):
                if failure_point == "after":
                    original(*args, **kwargs)
                raise OSError("independent migrated resume fault")

            monkeypatch.setattr(resumed.graph, "invoke", fail)
            with pytest.raises(OSError, match="migrated resume"):
                resumed.resolve_approval("t", approved, "reviewer")
            assert resumed.pending_approvals()[0]["state"] == "retryable"
            monkeypatch.setattr(resumed.graph, "invoke", original)
        answer = resumed.resolve_approval("t", approved, "reviewer")
        assert answer.status == "answered"
        resumed.store.refresh()
        assert len(resumed.store.refunds) == int(approved)
        task = values(resumed)["task"]
        assert task["active_order_id"] == "1001"
        if approved:
            assert task["status"] == "completed"
            assert task["verified_orders"]["1001"]["source_tool_call_id"] == "refund"
        else:
            assert task["status"] != "completed"
            assert "1001" not in task["verified_orders"] or task["verified_orders"]["1001"].get("status") != "refunded"
        terminal = [entry for entry in task["last_tool_outcomes"] if entry.get("source_tool_call_id") == "refund"]
        assert len(terminal) == 1
        assert terminal[0]["status"] == ("refunded" if approved else "denied")
        assert task["pending_action"] is None
        assert not resumed.pending_approvals()
        assert_closed(values(resumed)["messages"])
        projection = json.loads(model.requests[-1][0].content.split("\n")[-1])
        assert "pending_approval" not in projection
        if approved:
            assert projection["status"] == "completed"
        before = deepcopy(values(resumed))
        count = len(model.requests)
        assert resumed.resolve_approval("t", approved, "reviewer") == answer
        assert values(resumed) == before and len(model.requests) == count
    finally:
        resumed.close()
