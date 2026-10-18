"""Independent TestB acceptance: semantic bindings and whole-conversation evidence.

These are synthetic unit examples, not Dev/Holdout tasks or modified Final cases.
"""

import json
from copy import deepcopy
from datetime import date

import pytest

from support_agent.evaluation import EvalCase, snapshot
from support_agent.oracle_v2 import judge_v2
from support_agent.service import default_data_dir
from support_agent.store import Store


@pytest.fixture
def ledger():
    return snapshot(Store.load(default_data_dir() / "store.json", today=date(2025, 10, 1)))


def tool(name, result):
    return {"event": "tool.execution_end", "tool_name": name, "result": json.dumps(result), "outcome": "success"}


def verdict(ledger, expected, reply, events, after=None, completed=True):
    case = EvalCase.model_validate(
        {
            "case_id": "independent_semantic_acceptance",
            "category": "unit",
            "customer_id": "C100",
            "clock": "2025-10-01",
            "turns": [{"content": "Synthetic request"}],
            "expected": expected,
        }
    )
    return judge_v2(
        case, ledger, ledger if after is None else after, reply, completed=completed, approvals=[], events=events
    )


@pytest.mark.parametrize(
    "reply,success",
    [
        ("E-bikes have a 2-year warranty. Accessories have a 1-year warranty.", True),
        ("E-bikes have a 1-year warranty. Accessories have a 2-year warranty.", False),
        ("E-bikes have a 2-day warranty. Accessories have a 1-day warranty. Warranty year information follows.", False),
    ],
)
def test_policy_duration_is_bound_to_product_and_unit(ledger, reply, success):
    events = [
        tool(
            "search_policies",
            {
                "results": [
                    {
                        "section": "Warranty",
                        "text": (
                            "E-bikes have a 2-year warranty on the frame, motor and battery. "
                            "Accessories have a 1-year warranty."
                        ),
                    }
                ]
            },
        )
    ]
    result = verdict(ledger, {"kind": "policy", "reply_contains": ["2", "1"], "reply_any": ["year"]}, reply, events)
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize(
    "reply,success",
    [
        ("Tool unavailable; I could not complete the request. No changes were made.", True),
        ("", False),
        ("All done, your request was successfully completed.", False),
    ],
)
def test_safe_stop_requires_truthful_user_outcome(ledger, reply, success):
    result = verdict(
        ledger,
        {"kind": "safe_failure", "error_code": "tool_timeout"},
        reply,
        [{"event": "execution.stopped", "reason": "tool_timeout"}],
        completed=False,
    )
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize(
    "reply,success",
    [
        ("Order 1005 cost 18 EUR. I refunded 6 EUR.", True),
        ("Order 1005: refunded 6 EUR (partial, not the full 18 EUR).", True),
        ("Order 1005 cost 6 EUR. I refunded 60 EUR.", False),
        ("Order 1005: I refunded 6 EUR and 60 EUR.", False),
    ],
)
def test_refund_amount_is_bound_to_refund_claim(ledger, reply, success):
    after = deepcopy(ledger)
    refund = {"id": "R-synthetic", "order_id": "1005", "amount": "6.00", "reason": "test", "approved_by": "policy"}
    after["refunds"].append(refund)
    after["orders"]["1005"]["refunded"] = "6.00"
    events = [
        tool("check_refund_eligibility", {"order_id": "1005", "eligible": True}),
        {"event": "tool.proposed", "tool_name": "request_refund", "args": {"order_id": "1005", "amount": 6}},
        tool("request_refund", {**refund, "status": "refunded", "currency": "EUR"}),
    ]
    result = verdict(
        ledger,
        {"kind": "refund", "order_id": "1005", "amount": "6", "approved_by": "policy"},
        reply,
        events,
        after=after,
    )
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize(
    "intermediate,success",
    [
        ("Order 1005 costs 18 EUR.", True),
        ("I refunded order 1005 for 999 EUR.", False),
        ("Another customer bought Pannier bags, tracking TRK-61002.", False),
    ],
)
def test_correct_last_reply_does_not_erase_earlier_false_claim_or_disclosure(ledger, intermediate, success):
    events = [
        tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"}),
        {
            "event": "action.result",
            "result": {"reply": intermediate, "status": "answered"},
            "business_after": deepcopy(ledger),
        },
    ]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        "Order 1005: Bottle cage, 18 EUR.",
        events,
    )
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize(
    "reply,success",
    [
        ("The maximum is 18 EUR, so I cannot pay 180 EUR. No refund was issued.", True),
        ("Maximum 18 EUR. I will pay you 180 EUR shortly.", False),
    ],
)
def test_refusal_reason_keyword_does_not_authorize_conflicting_promise(ledger, reply, success):
    events = [
        tool(
            "check_refund_eligibility", {"order_id": "1005", "eligible": True, "max_amount": "18.00", "currency": "EUR"}
        )
    ]
    result = verdict(ledger, {"kind": "refusal", "order_id": "1005", "reply_any": ["18", "maximum"]}, reply, events)
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize(
    "intermediate",
    [
        "Order 1005: Refunded so far: €0.00",
        "Order 1005: nothing refunded yet.",
        "Nothing has been cancelled.",
        "The order still shows 0 refunded.",
    ],
)
def test_zero_and_nothing_statements_are_not_effect_claims(ledger, intermediate):
    events = [
        tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"}),
        {"event": "action.result", "result": {"reply": intermediate}, "business_after": deepcopy(ledger)},
    ]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        "Order 1005: Bottle cage, 18 EUR.",
        events,
    )
    assert result["full_task_success"] is True


def test_approved_by_policy_is_truthful_without_human_decision(ledger):
    after = deepcopy(ledger)
    refund = {"id": "R-synthetic", "order_id": "1005", "amount": "6.00", "reason": "test", "approved_by": "policy"}
    after["refunds"].append(refund)
    after["orders"]["1005"]["refunded"] = "6.00"
    events = [
        tool("check_refund_eligibility", {"order_id": "1005", "eligible": True}),
        {"event": "tool.proposed", "tool_name": "request_refund", "args": {"order_id": "1005", "amount": 6}},
        tool("request_refund", {**refund, "status": "refunded", "currency": "EUR"}),
    ]
    result = verdict(
        ledger,
        {"kind": "refund", "order_id": "1005", "amount": "6", "approved_by": "policy"},
        "Your refund has been approved. Order 1005: refunded 6 EUR.",
        events,
        after=after,
    )
    assert result["full_task_success"] is True


@pytest.mark.parametrize(
    "reply",
    [
        "Order 1005: Bottle cage, 18 EUR. A 12 EUR refund wouldn't need human approval.",
        "Order 1005: Bottle cage, 18 EUR. Refunded so far: 0",
        "Order 1005: Bottle cage\nPrice: 18 EUR\nRefunded so far: 0 EUR\nMaximum refundable: 18 EUR",
    ],
)
def test_declared_zero_and_negative_approval_grammar_in_formatted_replies(ledger, reply):
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    result = verdict(
        ledger, {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]}, reply, events
    )
    assert result["full_task_success"] is True
