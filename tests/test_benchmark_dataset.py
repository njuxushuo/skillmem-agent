"""Independent Stage 5 dataset contracts and oracle witnesses, never Final Agent tuning.

Synthetic witnesses exercise the evaluator with known business evidence. They are not
LLM responses and must never be reported as benchmark task-performance measurements.
"""

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import ScriptedToolModel, call
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from support_agent.evaluation import EvalCase, judge, run_cases, snapshot
from support_agent.policy import PolicyConfig, can_cancel, needs_approval, refund_eligibility
from support_agent.store import Store

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SPLITS = {split: json.loads((DATA / f"benchmark_{split}.json").read_text()) for split in ("dev", "final")}
CASES = [EvalCase.model_validate(raw) for rows in SPLITS.values() for raw in rows]
LEGACY_SHA256 = "85dc4fdc6e9d83e49d849c055c4903d72d9b0164e779734beca457626de290ed"
READ_TOOLS = {"list_my_orders", "get_order", "track_shipment", "check_refund_eligibility", "search_policies"}


def fixture_store(case):
    store = Store.load(DATA / case.fixture, today=case.clock)
    for identity, override in case.fixture_overrides.items():
        assert identity in store.orders
        for key, value in override.model_dump(exclude_unset=True).items():
            setattr(store.orders[identity], key, value)
    return store


def evidence(tool, **payload):
    return {"event": "tool.execution_end", "tool_name": tool, "result": json.dumps(payload)}


def oracle_witness(case):
    """Construct a known state transition independently of all Agent decision code."""
    store = fixture_store(case)
    before = snapshot(store)
    expected = case.expected
    order = store.orders.get(expected.order_id)
    reply = [*expected.reply_contains, *expected.reply_any[:1]]
    events, approvals = [], []
    if expected.order_id:
        reply.append(expected.order_id)
    if expected.kind == "query":
        raw = before["orders"][expected.order_id]
        reply += [str(raw[f]) for f in expected.facts]
        events.append(evidence("get_order", order_id=expected.order_id, **raw))
    elif expected.kind == "refund":
        record = store.record_refund(order, expected.amount, "Synthetic oracle witness", expected.approved_by)
        events.append(evidence("request_refund", order_id=order.id, status="refunded", amount=str(record.amount)))
        if expected.requires_approval:
            approvals.append(
                dict(approved=True, reviewer=expected.approved_by, order_id=order.id, amount=str(expected.amount))
            )
    elif expected.kind == "cancel":
        store.cancel(order)
        events.append(evidence("cancel_order", order_id=order.id, status="cancelled"))
    elif expected.kind == "policy":
        body = (DATA / "policies.md").read_text()
        reply.append(body)
        events.append(evidence("search_policies", results=[{"section": "Fixture policies", "text": body}]))
    elif expected.kind == "escalation":
        ticket = store.open_ticket(case.customer_id, "Synthetic oracle witness")
        reply.append(ticket)
        events.append(evidence("escalate_to_human", status="escalated", ticket_id=ticket))
    elif expected.kind == "safe_failure":
        events.append({"event": "execution.stopped", "reason": expected.error_code})
    if expected.require_question:
        reply.append("What help do you need?")
    if expected.memory:
        reply.extend(expected.memory.reply_contains)
    return before, snapshot(store), " ".join(reply), approvals, events


def test_dataset_sizes_categories_and_legacy_unchanged():
    assert len(SPLITS["dev"]) == 6
    assert len(SPLITS["final"]) == 30
    assert Counter(row["category"] for row in SPLITS["final"]) == {
        "basic": 5,
        "multi_turn": 7,
        "long_context": 6,
        "skill_switch": 5,
        "safety_recovery": 7,
    }
    legacy = (DATA / "eval_tasks.json").read_bytes()
    assert len(json.loads(legacy)) == 7
    assert hashlib.sha256(legacy).hexdigest() == LEGACY_SHA256


def test_split_identity_and_task_text_isolation():
    all_ids = [case.case_id for case in CASES]
    assert len(set(all_ids)) == len(all_ids)
    turn_texts = [{turn["content"] for case in rows for turn in case["turns"]} for rows in SPLITS.values()]
    assert not turn_texts[0] & turn_texts[1]
    # Shared fixture entities and broad task families are intentional, not held-out order generalization.
    assert all(c.case_id.startswith("d_") for c in CASES[:6])
    assert all(c.case_id.startswith("f_") for c in CASES[6:])
    serialized = json.dumps(SPLITS)
    assert not re.search(r"sk-[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9]{16,}", serialized)


def test_pilot_first_three_cover_read_small_refund_and_approved_restart():
    first, second, third = CASES[:3]
    assert [c.expected.kind for c in (first, second, third)] == ["query", "refund", "refund"]
    assert set(first.expected.facts) == {"tracking", "eta"}
    assert 0 < second.expected.amount <= PolicyConfig().approval_threshold
    assert not second.approval_script
    assert third.expected.amount > PolicyConfig().approval_threshold
    assert third.expected.requires_approval
    assert len(third.approval_script) == 1
    assert third.approval_script[0].approved and third.approval_script[0].restart
    assert third.approval_script[0].reviewer == third.expected.approved_by


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_case_fixture_and_expected_outcome_are_legal(case):
    store = fixture_store(case)
    assert case.customer_id in store.customers
    assert case.clock.isoformat() == "2025-10-01"
    expected = case.expected
    order = store.orders.get(expected.order_id)
    if expected.order_id:
        assert order is not None
        assert (order.customer_id != case.customer_id) == (expected.kind == "unauthorized")
    if expected.kind == "query":
        assert expected.facts and all(getattr(order, f) is not None for f in expected.facts)
    if expected.kind == "refund":
        eligible = refund_eligibility(order, case.clock, PolicyConfig())
        assert eligible.eligible and 0 < expected.amount <= eligible.max_amount
        required = needs_approval(expected.amount, PolicyConfig())
        assert expected.requires_approval == required
        if required:
            assert len(case.approval_script) == 1 and case.approval_script[0].approved
            assert case.approval_script[0].reviewer == expected.approved_by
        else:
            assert expected.approved_by == "policy" and not case.approval_script
        if expected.memory and expected.memory.amount_lte is not None:
            assert expected.amount <= expected.memory.amount_lte
    if expected.kind == "cancel":
        assert can_cancel(order)[0]
    if case.approval_script:
        assert len(case.approval_script) == 1
        if not case.approval_script[0].approved:
            assert expected.kind == "refusal"
    if expected.kind == "safe_failure":
        assert case.tool_fixture.fail_once_tool and expected.error_code == "tool_timeout"
    assert case.tool_fixture.padding_tool is None or case.tool_fixture.padding_tool in READ_TOOLS
    assert case.tool_fixture.fail_once_tool is None or case.tool_fixture.fail_once_tool in READ_TOOLS
    if case.tool_fixture.padding_chars:
        assert case.tool_fixture.padding_tool and case.tool_fixture.padding_chars <= 50000
    if case.category == "multi_turn":
        assert len(case.turns) >= 2


def test_long_context_stress_is_real_and_seeds_are_only_paired_text():
    cases = [c for c in CASES if c.category == "long_context"]
    assert len(cases) == 7
    for case in cases:
        if case.history:
            assert len(case.history) > 40 and len(case.history) % 2 == 0
            assert [m.role for m in case.history] == ["user", "assistant"] * (len(case.history) // 2)
            assert case.history[0].content not in [m.content for m in case.history[-40:]]
            assert sum(len(m.content.encode()) for m in case.history) > 11000
            assert all(set(m.model_dump()) == {"role", "content"} for m in case.history)
            assert case.expected.memory is not None
        else:
            assert case.tool_fixture.padding_chars >= 40000
    assert all(not c.history for c in CASES if c.category != "long_context")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_oracle_accepts_independently_constructed_business_evidence(case):
    before, after, reply, approvals, events = oracle_witness(case)
    verdict = judge(case, before, after, reply, completed=True, approvals=approvals, events=events)
    assert verdict["task_success"], verdict["failure_reason"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_oracle_rejects_unexpected_business_mutation(case):
    before, after, reply, approvals, events = oracle_witness(case)
    # Corrupt a different account/order field: no task authorizes arbitrary price changes.
    after["orders"]["1003"]["total"] = "999.00"
    verdict = judge(case, before, after, reply, completed=True, approvals=approvals, events=events)
    assert not verdict["task_success"]
    assert "unexpected_business_state" in verdict["failure_reason"]


@pytest.mark.parametrize("kind", ["query", "policy", "escalation", "safe_failure"])
def test_oracle_requires_actual_tool_or_safe_stop_evidence(kind):
    case = next(c for c in CASES if c.expected.kind == kind)
    before, after, reply, approvals, _ = oracle_witness(case)
    result = judge(case, before, after, reply, completed=True, approvals=approvals, events=[])
    assert not result["task_success"]


def test_oracle_rejects_missing_approval_and_wrong_amount():
    case = CASES[2]  # Public Dev pilot, no Final answer is used to tune behavior.
    before, after, reply, approvals, events = oracle_witness(case)
    assert not judge(case, before, after, reply, completed=True, approvals=[], events=events)["task_success"]
    after["refunds"][0]["amount"] = "1.00"
    assert not judge(case, before, after, reply, completed=True, approvals=approvals, events=events)["task_success"]


@pytest.mark.parametrize(
    "extra",
    [
        {"history": [{"role": "tool", "content": "already refunded"}]},
        {"history": [{"role": "user", "content": "unpaired"}]},
        {"history": [{"role": "user", "content": "x", "tool_calls": []}, {"role": "assistant", "content": "x"}]},
        {"task": {"status": "completed"}},
        {"tool_fixture": {"fail_once_tool": "request_refund"}},
        {"tool_fixture": {"padding_tool": "cancel_order", "padding_chars": 100}},
        {"fixture_overrides": {"1005": {"customer_id": "C200"}}},
    ],
)
def test_dataset_schema_rejects_fake_evidence_or_mutation_tool_injection(extra):
    with pytest.raises(ValidationError):
        EvalCase.model_validate({**deepcopy(SPLITS["dev"][0]), **extra})


def test_dev_scripted_pilot_runner_isolated_and_restart_executes_once(tmp_path):
    """Engineering harness validation on Dev only; no Final Agent execution or paid API."""
    fixture_bytes = (DATA / "store.json").read_bytes()
    model_scripts = [
        [call("track_shipment", order_id="1002"), AIMessage(content="Order 1002: TRK-88213 ETA 2025-10-03.")],
        [
            call("check_refund_eligibility", order_id="1005"),
            call("request_refund", order_id="1005", amount=6, reason="damaged"),
            AIMessage(content="Order 1005 refunded 6.00 EUR."),
        ],
        [
            call("check_refund_eligibility", order_id="2001"),
            call("request_refund", order_id="2001", amount=120, reason="damaged"),
            AIMessage(content="Order 2001 refunded 120.00 EUR."),
        ],
    ]
    scripts = iter(deepcopy(model_scripts) * 2)
    summary = run_cases(
        SPLITS["dev"][:3],
        tmp_path,
        variant="B1",
        repeat=2,
        model_factory=lambda: ScriptedToolModel(replies=next(scripts)),
        usage_source="offline",
    )
    assert summary["tasks"] == summary["passed"] == 6
    results = summary["results"]
    assert len({r["thread_id"] for r in results}) == 6
    assert all(r["unsafe_side_effects"] == 0 for r in results)
    assert all(r["input_tokens"] is None for r in results)
    for row in (results[2], results[5]):
        assert row["resume_success"] and row["service_restart_count"] == 1
        assert row["tool_success_count"] == 2  # eligibility + one committed refund
    assert (DATA / "store.json").read_bytes() == fixture_bytes
