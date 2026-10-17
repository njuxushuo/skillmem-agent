"""Independent G4 tests: real graph/service and SDK MockTransport, always offline."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field
from test_deepseek import mock_model, response
from test_stage3_acceptance import AcceptanceModel, assert_closed, proposed, values

from support_agent.service import ConversationError, SupportService
from support_agent.skills import load_skills
from support_agent.tracing import TraceRecorder

EXPECTED = {
    "order_shipping": {"list_my_orders", "get_order", "track_shipment"},
    "refund": {"list_my_orders", "get_order", "check_refund_eligibility", "request_refund"},
    "cancel": {"list_my_orders", "get_order", "cancel_order"},
    "policy_support": {"search_policies", "escalate_to_human"},
}
PUBLIC = {"select_skill", "escalate_to_human"}
BUSINESS = set.union(*EXPECTED.values())


class ScopedModel(AcceptanceModel):
    request_tool_names: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.request_tool_names.append(set(self.bound_names))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def select(skill, identity="select"):
    return proposed("select_skill", identity, skill_id=skill)


@pytest.mark.parametrize("skill", EXPECTED)
def test_registry_mapping_matches_effective_per_request_schema_and_sop(tmp_path, skill):
    registry = load_skills(Path("data"))
    assert set(registry) == set(EXPECTED)
    assert set(registry[skill].tool_names) == EXPECTED[skill]
    assert registry[skill].sop_path.read_text() == registry[skill].sop
    model = ScopedModel(replies=[select(skill), AIMessage("Ready")])
    service = SupportService(variant="MS", model=model, db_path=str(tmp_path / "scope.sqlite"))
    try:
        assert service.chat("C100", "Help with this service", "t").status == "answered"
        assert model.request_tool_names == [PUBLIC, PUBLIC | EXPECTED[skill]]
        assert values(service)["active_skill"] == skill
        assert registry[skill].sop in model.requests[1][0].content
        for other in set(EXPECTED) - {skill}:
            assert registry[other].sop not in model.requests[1][0].content
        assert all(spec.sop not in model.requests[0][0].content for spec in registry.values())
        assert_closed(values(service)["messages"])
    finally:
        service.close()


@pytest.mark.parametrize("skill", [None, "order_shipping", "policy_support", "cancel"])
def test_guessing_refund_tool_is_rejected_by_executor_not_just_schema(tmp_path, skill):
    replies = ([select(skill)] if skill else []) + [
        proposed("request_refund", "forbidden", order_id="1005", amount=18, reason="broken"),
        AIMessage("Cannot"),
    ]
    trace = TraceRecorder(tmp_path / "guard.jsonl", run_id="guard", thread_id="t", source="mock")
    model = ScopedModel(replies=replies)
    service = SupportService(variant="MS", model=model, trace=trace, db_path=str(tmp_path / "guard.sqlite"))
    try:
        assert service.chat("C100", "Refund order 1005", "t").status == "answered"
        assert not service.store.refunds and service.store.orders["1005"].refunded == 0
        result = next(
            m for m in values(service)["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "forbidden"
        )
        assert result.status == "error" and json.loads(result.content)["error"] == "tool_not_allowed_for_skill"
        assert trace.statistics()["tool_executed_count"] == 0
        assert "request_refund" not in model.request_tool_names[-1]
        assert_closed(values(service)["messages"])
    finally:
        service.close()
        trace.close()


@pytest.mark.parametrize("unknown", ["missing-skill", "../../etc/passwd"])
def test_unknown_skill_keeps_state_and_pairs_error(tmp_path, unknown):
    model = ScopedModel(replies=[select(unknown), AIMessage("Clarify")])
    service = SupportService(variant="MS", model=model, db_path=str(tmp_path / "unknown.sqlite"))
    try:
        assert service.chat("C100", "Help", "t").status == "answered"
        state = values(service)
        assert state.get("active_skill") is None
        result = next(m for m in state["messages"] if isinstance(m, ToolMessage))
        assert result.tool_call_id == "select" and result.status == "error" and "unknown_skill" in result.content
        assert model.request_tool_names == [PUBLIC, PUBLIC]
        assert not service.store.refunds
    finally:
        service.close()


def test_selection_plus_mutation_multicall_executes_neither_and_closes_all_ids(tmp_path):
    batch = AIMessage(
        "",
        tool_calls=[
            *select("refund", "choose").tool_calls,
            *proposed("request_refund", "pay", order_id="1005", amount=18, reason="broken").tool_calls,
        ],
    )
    model = ScopedModel(replies=[batch, AIMessage("Please retry")])
    trace = TraceRecorder(tmp_path / "multi.jsonl", run_id="multi", thread_id="t", source="mock")
    service = SupportService(variant="MS", model=model, trace=trace, db_path=str(tmp_path / "multi.sqlite"))
    try:
        assert service.chat("C100", "Refund", "t").status == "answered"
        state = values(service)
        results = [m for m in state["messages"] if isinstance(m, ToolMessage)]
        assert {m.tool_call_id for m in results} == {"choose", "pay"}
        assert len(results) == 2 and all("multiple_tool_calls_not_allowed" in m.content for m in results)
        assert state.get("active_skill") is None and not service.store.refunds
        assert trace.statistics()["total_tool_executed_count"] == 0
        assert_closed(state["messages"])
    finally:
        service.close()
        trace.close()


def test_three_skill_transitions_preserve_fact_source_and_invalidate_changed_order(tmp_path):
    model = ScopedModel(
        replies=[
            select("order_shipping", "s1"),
            proposed("get_order", "old-order", order_id="1001"),
            AIMessage("Order found"),
            select("refund", "s2"),
            proposed("check_refund_eligibility", "elig", order_id="1001"),
            AIMessage("Eligible"),
            select("order_shipping", "s3"),
            proposed("track_shipment", "track", order_id="1002"),
            AIMessage("Shipping"),
        ]
    )
    service = SupportService(variant="MS", model=model, db_path=str(tmp_path / "switch.sqlite"))
    try:
        assert service.chat("C100", "Inspect order 1001", "t").status == "answered"
        assert service.chat("C100", "Check refund eligibility for order 1001", "t").status == "answered"
        assert values(service)["task"]["eligibility"]["source_tool_call_id"] == "elig"
        assert service.chat("C100", "Switch to order 1002; only track shipping", "t").status == "answered"
        state = values(service)
        assert state["active_skill"] == "order_shipping"
        assert state["task"]["active_order_id"] == "1002" and state["task"]["eligibility"] is None
        assert state["task"]["verified_orders"]["1001"]["source_tool_call_id"] == "old-order"
        assert not service.store.refunds
        assert model.request_tool_names[-1] == PUBLIC | EXPECTED["order_shipping"]
        assert_closed(state["messages"])
    finally:
        service.close()


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("failure_point", ["before", "after"])
def test_approval_freezes_skill_then_terminal_result_allows_switch(tmp_path, monkeypatch, approved, failure_point):
    model = ScopedModel(
        replies=[
            select("refund", "s-refund"),
            proposed("check_refund_eligibility", "elig", order_id="1001"),
            proposed("request_refund", "pay", order_id="1001", amount=89, reason="broken"),
            select("order_shipping", "s-shipping"),
            proposed("get_order", "after-refund", order_id="1001"),
            AIMessage("Done"),
        ]
    )
    service = SupportService(variant="MS", model=model, db_path=str(tmp_path / "approval.sqlite"))
    try:
        assert service.chat("C100", "Refund order 1001", "t").status == "awaiting_approval"
        before = deepcopy(values(service))
        with pytest.raises(ConversationError) as error:
            service.chat("C100", "Switch to shipping for order 1002", "t")
        assert error.value.status_code == 409 and values(service) == before
        assert before["active_skill"] == "refund" and not service.store.refunds
        original = service.graph.invoke

        def fail(*args, **kwargs):
            if failure_point == "after":
                original(*args, **kwargs)
            raise OSError("stage4 independent fault")

        monkeypatch.setattr(service.graph, "invoke", fail)
        with pytest.raises(OSError, match="stage4 independent"):
            service.resolve_approval("t", approved, "reviewer")
        assert service.pending_approvals()[0]["state"] == "retryable"
        monkeypatch.setattr(service.graph, "invoke", original)
        answer = service.resolve_approval("t", approved, "reviewer")
        assert answer.status == "answered"
        assert values(service)["active_skill"] == "order_shipping"
        service.store.refresh()
        assert len(service.store.refunds) == int(approved)
        assert_closed(values(service)["messages"])
        requests = len(model.requests)
        assert service.resolve_approval("t", approved, "reviewer") == answer
        assert len(model.requests) == requests
    finally:
        service.close()


@pytest.mark.parametrize("variant", ["B1", "M"])
@pytest.mark.parametrize("full_sop", [False, True])
def test_baselines_stay_full_business_tools_with_fair_sop_toggle(tmp_path, variant, full_sop):
    model = ScopedModel()
    service = SupportService(variant=variant, full_sop=full_sop, model=model, db_path=str(tmp_path / "baseline.sqlite"))
    try:
        assert service.chat("C100", "Hello", "t").status == "answered"
        assert model.request_tool_names == [BUSINESS] and "select_skill" not in model.bound_names
        for spec in load_skills(Path("data")).values():
            assert (spec.sop in model.requests[0][0].content) is full_sop
        assert values(service).get("active_skill") is None
    finally:
        service.close()


def test_real_sdk_mock_dynamic_schema_and_all_router_overhead_are_counted(monkeypatch, tmp_path):
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    responses = [
        response(calls=[("unknown", "select_skill", {"skill_id": "missing"})], usage=usage),
        response(calls=[("s1", "select_skill", {"skill_id": "order_shipping"})], usage=usage),
        response(calls=[("same", "select_skill", {"skill_id": "order_shipping"})], usage=usage),
        response(
            calls=[("denied", "request_refund", {"order_id": "1005", "amount": 18, "reason": "broken"})], usage=usage
        ),
        response(calls=[("s2", "select_skill", {"skill_id": "refund"})], usage=usage),
        response(calls=[("elig", "check_refund_eligibility", {"order_id": "1005"})], usage=usage),
        response("Eligible; no payment performed", usage=usage),
    ]
    model, requests, client = mock_model(monkeypatch, responses)
    trace = TraceRecorder(tmp_path / "sdk.jsonl", run_id="sdk", thread_id="t", source="mock")
    service = SupportService(variant="MS", model=model, trace=trace, db_path=str(tmp_path / "sdk.sqlite"))
    try:
        assert service.chat("C100", "Check eligibility for order 1005", "t").status == "answered"
        expected = [
            PUBLIC,
            PUBLIC,
            PUBLIC | EXPECTED["order_shipping"],
            PUBLIC | EXPECTED["order_shipping"],
            PUBLIC | EXPECTED["order_shipping"],
            PUBLIC | EXPECTED["refund"],
            PUBLIC | EXPECTED["refund"],
        ]
        assert [{t["function"]["name"] for t in request["tools"]} for request in requests] == expected
        stats = trace.statistics()
        assert stats["model_calls"] == 7
        assert (stats["input_tokens"], stats["output_tokens"], stats["total_tokens"]) == (700, 70, 770)
        assert stats["skill_model_calls"] == 4
        assert stats["skill_routing_count"] == stats["control_executed_count"] == 4
        assert stats["skill_switch_count"] == 2 and stats["skill_rejected_count"] == 1
        assert stats["tool_executed_count"] == 1 and stats["total_tool_executed_count"] == 5
        assert stats["skill_input_tokens"] == 400 and stats["skill_total_tokens"] == 440
        assert stats["skill_routing_latency_ms"] >= 0
        assert not service.store.refunds
        assert_closed(values(service)["messages"])
        tool_results = [m for m in requests[-1]["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_results] == ["unknown", "s1", "same", "denied", "s2", "elig"]
        assert all(event.get("usage_source") == "mock" for event in trace.events if event["event"] == "model.response")
    finally:
        service.close()
        trace.close()
        client.close()


@pytest.mark.parametrize("old_variant", ["B1", "M"])
@pytest.mark.parametrize("approved", [True, False])
def test_pending_older_variant_can_resume_in_ms_without_weakening_allowlist(tmp_path, old_variant, approved):
    db = str(tmp_path / "migration.sqlite")
    previous = SupportService(
        variant=old_variant,
        db_path=db,
        model=ScopedModel(
            replies=[
                proposed("check_refund_eligibility", "elig", order_id="1001"),
                proposed("request_refund", "pay", order_id="1001", amount=89, reason="broken"),
            ]
        ),
    )
    try:
        assert previous.chat("C100", "Refund order 1001", "t").status == "awaiting_approval"
        assert previous.graph.get_state(previous._config("C100", "t")).interrupts
    finally:
        previous.close()
    model = ScopedModel()
    current = SupportService(variant="MS", model=model, db_path=db)
    try:
        assert current.resolve_approval("t", approved, "reviewer").status == "answered"
        assert values(current)["active_skill"] == "refund"
        assert model.request_tool_names == [PUBLIC | EXPECTED["refund"]]
        assert len(current.store.refunds) == int(approved)
        if approved:
            assert values(current)["task"]["status"] == "completed"
        assert_closed(values(current)["messages"])
        assert not current.pending_approvals()
    finally:
        current.close()


@pytest.mark.parametrize("args", [{}, {"skill_id": 123}, {"skill_id": "refund", "customer_id": "C200"}])
def test_bad_selection_parameters_cannot_change_skill(tmp_path, args):
    trace = TraceRecorder(tmp_path / "params.jsonl", run_id="params", thread_id="t", source="mock")
    model = ScopedModel(replies=[proposed("select_skill", "bad", **args), AIMessage("Clarify")])
    service = SupportService(variant="MS", model=model, trace=trace, db_path=str(tmp_path / "params.sqlite"))
    try:
        assert service.chat("C100", "Help", "t").status == "answered"
        state = values(service)
        result = next(m for m in state["messages"] if isinstance(m, ToolMessage))
        assert result.tool_call_id == "bad" and result.status == "error"
        assert "invalid_parameters" in result.content
        assert state.get("active_skill") is None
        assert trace.statistics()["total_tool_executed_count"] == 0
        assert_closed(state["messages"])
    finally:
        service.close()
        trace.close()


def test_ambiguous_intent_can_end_in_clarification_without_guessing_skill(tmp_path):
    model = ScopedModel(replies=[AIMessage("Do you want shipping information or a refund?")])
    service = SupportService(variant="MS", model=model, db_path=str(tmp_path / "clarify.sqlite"))
    try:
        answer = service.chat("C100", "I need help with something about my purchase", "t")
        assert answer.status == "answered" and "?" in answer.reply
        assert values(service).get("active_skill") is None
        assert not service.store.refunds and not service.store.tickets
        assert model.request_tool_names == [PUBLIC]
    finally:
        service.close()
