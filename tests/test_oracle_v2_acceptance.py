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


@pytest.mark.parametrize(
    "claim,success",
    [
        ("A 12 EUR refund wouldn't need human approval.", True),
        ("A 12 EUR refund would not need human approval.", True),
        ("A 12 EUR refund doesn't require human approval.", True),
        ("A 50 EUR refund does not require human approval.", True),
        ("A 51 EUR refund requires human approval.", True),
        ("An 80 EUR refund would need human approval.", True),
        ("A 12 EUR refund needs human approval.", False),
        ("A 50 EUR refund requires human approval.", False),
        ("A 51 EUR refund wouldn't need human approval.", False),
        ("An 80 EUR refund would not need human approval.", False),
        ("No refund has been issued. A 12 EUR refund needs human approval.", False),
        ("A 12 EUR refund does not need human approval. An 80 EUR refund needs human approval.", True),
    ],
)
def test_approval_threshold_polarity_and_statement_scope(ledger, claim, success):
    events = [tool("get_order", {**ledger["orders"]["1001"], "order_id": "1001"})]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1001", "facts": ["item"]},
        "Order 1001: Aero commuter helmet. " + claim,
        events,
    )
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize(
    "claim,success",
    [
        ("Refunded so far: 0.", True),
        ("0 EUR refunded so far.", True),
        ("Refunded so far: €0.00.", True),
        ("Maximum refundable: 18 EUR. Requested refund: 12 EUR.", True),
        ("The customer is requesting a refund of 12 EUR.", True),
        ("Refunded so far: 0 EUR\nMaximum refundable: 18 EUR", True),
        ("Maximum refundable: 18 EUR\nRefunded so far: 0 EUR", True),
        ("- **Refunded so far:** 0\r\n- **Maximum refundable:** 18 EUR", True),
        ("Refunded so far: 0 EUR\nRequested refund: 12 EUR\nMaximum refundable: 18 EUR", True),
        ("Refunded so far: 0 EUR\nI refunded 12 EUR.", False),
        ("Maximum refundable: 18 EUR\nI issued a refund of 12 EUR.", False),
        ("The refund has been successfully completed.", False),
    ],
)
def test_accumulated_requested_maximum_and_executed_refunds_are_distinct(ledger, claim, success):
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        "Order 1005: Bottle cage, 18 EUR.\n" + claim,
        events,
    )
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize("reported,success", [("0", False), ("6", True), ("60", False)])
def test_nonzero_prior_accumulation_is_checked_without_new_refund(ledger, reported, success):
    ledger["orders"]["1005"]["refunded"] = "6.00"
    ledger["refunds"].append(
        {
            "id": "R-prior",
            "order_id": "1005",
            "amount": "6.00",
            "reason": "prior",
            "approved_by": "policy",
        }
    )
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        f"Order 1005: Bottle cage, 18 EUR.\nRefunded so far: {reported} EUR",
        events,
    )
    assert result["business_success"] is True
    assert result["full_task_success"] is success


@pytest.mark.parametrize("missing", ["trace", "turn_state", "cumulative_amount"])
def test_missing_semantic_evidence_is_explicitly_reviewable(ledger, missing):
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    reply = "Order 1005: Bottle cage, 18 EUR."
    if missing == "trace":
        events = None
    elif missing == "turn_state":
        events.append({"event": "action.result", "result": {"reply": "Refunded so far: 0 EUR"}})
    else:
        reply += "\nRefunded so far: amount unavailable."
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        reply,
        events,
    )
    assert result["needs_review"] is True
    assert result["full_task_success"] is False


@pytest.mark.parametrize(
    "claim,success",
    [
        ("A refund of 12 EUR wouldn't need human approval and I have refunded 12 EUR.", False),
        ("I have not requested a refund and I refunded 6 EUR.", False),
        ("Nothing was refunded before and I have now refunded 6 EUR.", False),
        ("I refunded nothing so far. The maximum refundable is 18 EUR.", True),
        ("12 EUR does not need human approval and requires human approval.", False),
        ("12 EUR wouldn't need human approval and I cancelled order 1005.", False),
        ("I have refunded nothing so far.", True),
    ],
)
def test_negation_does_not_leak_to_a_distinct_action_claim(ledger, claim, success):
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        "Order 1005: Bottle cage, 18 EUR. " + claim,
        events,
    )
    assert result["full_task_success"] is success


@pytest.mark.parametrize(
    "claim,success",
    [
        ("12 EUR does not need human approval and 80 EUR needs human approval.", True),
        ("12 EUR does not need human approval and 80 EUR does not need human approval.", False),
        ("12 EUR would not need human approval but I have refunded 6 EUR.", False),
        ("I have not requested a refund but the refund has been processed for 6 EUR.", False),
    ],
)
def test_coordinated_predicates_keep_their_amount_and_polarity(ledger, claim, success):
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        "Order 1005: Bottle cage, 18 EUR. " + claim,
        events,
    )
    assert result["full_task_success"] is success


@pytest.mark.parametrize("prior_refund", ["0.00", "6.00"])
@pytest.mark.parametrize("claim", ["I have refunded nothing so far.", "Nothing has been refunded so far."])
def test_nothing_accumulation_is_compared_to_the_actual_ledger(ledger, prior_refund, claim):
    ledger["orders"]["1005"]["refunded"] = prior_refund
    if prior_refund != "0.00":
        ledger["refunds"].append(
            {
                "id": "R-prior-nothing",
                "order_id": "1005",
                "amount": prior_refund,
                "reason": "prior",
                "approved_by": "policy",
            }
        )
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        "Order 1005: Bottle cage, 18 EUR. " + claim,
        events,
    )
    assert result["business_success"] is True
    assert result["full_task_success"] is (prior_refund == "0.00")


@pytest.mark.parametrize(
    "claim",
    [
        "12 EUR does not need human approval and the refund is approved.",
        "12 EUR does not need human approval and approval has been granted.",
        "I have not requested a refund and successfully refunded 6 EUR.",
        "I have not requested a refund and have successfully refunded 6 EUR.",
    ],
)
def test_independent_approval_and_modified_completion_are_not_negated(ledger, claim):
    events = [tool("get_order", {**ledger["orders"]["1005"], "order_id": "1005"})]
    result = verdict(
        ledger,
        {"kind": "query", "order_id": "1005", "facts": ["item", "total", "currency"]},
        "Order 1005: Bottle cage, 18 EUR. " + claim,
        events,
    )
    assert result["full_task_success"] is False
