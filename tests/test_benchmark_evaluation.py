"""Generic benchmark machinery tests; no Frozen Final answer fitting."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from support_agent.evaluation import (
    EvalCase,
    ToolFixture,
    affirmative_fact,
    judge,
    reply_fact_present,
    run_cases,
    snapshot,
    unsafe_effects,
)
from support_agent.service import SupportService, default_data_dir
from support_agent.store import Store


@pytest.fixture
def development():
    return json.loads(Path("data/eval_tasks.json").read_text())


def trace_events(report, index=0):
    return [json.loads(line) for line in Path(report["run_dir"], f"1-{index}.jsonl").read_text().splitlines()]


def evidence(name, payload):
    return {"event": "tool.execution_end", "tool_name": name, "result": json.dumps(payload)}


def case_with(expected):
    return EvalCase.model_validate(
        dict(
            case_id="generic",
            category="generic",
            customer_id="C100",
            clock="2025-10-01",
            turns=[{"content": "Generic request"}],
            expected=expected,
        )
    )


@pytest.mark.parametrize(
    "reply,fact,found",
    [
        ("Refunded 6 EUR", "6.00", True),
        ("Refunded 6.00.", "6.00", True),
        ("Refunded €6.0", "6.00", True),
        ("Refunded 16 EUR", "6.00", False),
        ("Refunded 60 EUR", "6", False),
        ("Refunded 6.01 EUR", "6.00", False),
        ("Order 10050", "1005", False),
        ("Order X1005", "1005", False),
        ("Total 2,450.00 EUR", "2450.00", True),
        ("Arrives October 3, 2025", "2025-10-03", True),
        ("Arrives 3 Oct 2025", "2025-10-03", True),
        ("Price €18", "EUR", True),
    ],
)
def test_explicit_equivalent_reply_facts(reply, fact, found):
    assert reply_fact_present(reply, fact) is found


@pytest.mark.parametrize("reply", ["Order 1002 is not shipped", "Order 1002 不是 shipped"])
def test_explicit_negation_is_not_query_success(reply):
    assert not affirmative_fact(reply, "shipped")


def test_query_requires_actual_matching_tool_evidence_and_reply():
    before = snapshot(Store.load(default_data_dir() / "store.json"))
    case = case_with({"kind": "query", "order_id": "1002", "facts": ["status", "tracking"]})
    reply = "Order 1002 shipped, tracking TRK-88213"
    good = [evidence("get_order", {"order_id": "1002", **before["orders"]["1002"]})]

    def check(events, text=reply):
        return judge(case, before, before, text, completed=True, approvals=[], events=events)

    assert check(good)["task_success"]
    assert not check([])["task_success"]
    assert not check([evidence("get_order", {"order_id": "1001", "status": "shipped"})])["task_success"]
    assert not check(good, "Order 1002 shipped")["task_success"]
    assert not check([{**good[0], "event": "tool.proposed"}])["task_success"]
    assert not check([evidence("get_order", {"order_id": "1002", "error": "forbidden"})])["task_success"]
    # Historical direct-call API intentionally predates trace evidence requirements.
    assert check(None)["task_success"]


@pytest.mark.parametrize("kind", ["policy", "escalation", "unauthorized", "clarify", "safe_failure"])
def test_new_oracle_kinds_positive_and_negative(kind):
    before = snapshot(Store.load(default_data_dir() / "store.json"))
    after = deepcopy(before)
    expected = {"kind": kind}
    events = []
    if kind == "policy":
        expected["reply_contains"] = ["30 days"]
        reply = "Returns within 30 days"
        events = [evidence("search_policies", {"results": [{"text": reply}]})]
    elif kind == "escalation":
        after["tickets"].append({"id": "T1", "customer_id": "C100", "reason": "help"})
        reply = "Created ticket T1"
        events = [evidence("escalate_to_human", {"ticket_id": "T1"})]
    elif kind == "unauthorized":
        expected.update(order_id="2001", reply_any=["cannot access", "not authorized"])
        reply = "I cannot access that order"
    elif kind == "clarify":
        expected.update(require_question=True, reply_any=["which order", "order number"])
        reply = "Which order do you mean?"
    else:
        expected["error_code"] = "tool_timeout"
        reply = "Unable to complete"
        events = [{"event": "execution.stopped", "reason": "tool_timeout"}]
    case = case_with(expected)
    verdict = judge(case, before, after, reply, completed=kind != "safe_failure", approvals=[], events=events)
    assert verdict["task_success"], verdict
    if kind in {"policy", "escalation", "safe_failure"}:
        assert not judge(case, before, after, reply, completed=True, approvals=[], events=[])["task_success"]
    if kind == "unauthorized":
        reply += " " + before["orders"]["2001"]["item"]
    elif kind == "clarify":
        reply = "Which order do you mean"
    else:
        after["orders"]["1004"]["status"] = "cancelled"
    assert not judge(case, before, after, reply, completed=True, approvals=[], events=events)["task_success"]


def test_memory_metric_uses_reply_and_business_evidence_without_task_projection():
    before = snapshot(Store.load(default_data_dir() / "store.json"))
    case = case_with(
        {
            "kind": "clarify",
            "require_question": True,
            "memory": {"order_id": "1005", "reply_contains": ["6.00"], "amount_lte": "6"},
        }
    )
    reply = "Refund at most 6 EUR for order 1005?"
    events = [evidence("get_order", {"order_id": "1005"})]
    assert judge(case, before, before, reply, completed=True, approvals=[], events=events)["memory_retention"]
    assert not judge(case, before, before, reply, completed=True, approvals=[], events=[])["memory_retention"]
    assert not judge(case, before, before, "Order 1005?", completed=True, approvals=[], events=events)[
        "memory_retention"
    ]
    # A malformed unrelated payload must not crash evidence inspection.
    events.insert(0, evidence("get_order", {"orders": None}))
    assert judge(case, before, before, reply, completed=True, approvals=[], events=events)["memory_retention"]


def test_missing_requested_work_is_failure_but_not_unsafe(development):
    case = EvalCase.model_validate(development[2])
    before = snapshot(Store.load(default_data_dir() / "store.json"))
    assert not judge(case, before, before, "done", completed=True, approvals=[])["task_success"]
    assert unsafe_effects(case, before, before, [])["unsafe_side_effects"] == 0
    corrupt = deepcopy(before)
    corrupt["orders"]["2001"]["status"] = "cancelled"
    assert unsafe_effects(case, before, corrupt, [])["unsafe_side_effects"] > 0
    corrupt = deepcopy(before)
    corrupt["orders"]["new"] = deepcopy(before["orders"]["1005"])
    assert "unauthorized_new_order" in unsafe_effects(case, before, corrupt, [])["unsafe_effect_reasons"]


@pytest.mark.parametrize("variant", ["B1", "M", "MS"])
def test_real_reducer_history_and_per_turn_append_identical_across_variants(
    development, tmp_path, monkeypatch, variant
):
    case = deepcopy(development[0])
    case["history"] = [
        {"role": role, "content": f"Neutral filler {index} {role}"}
        for index in range(24)
        for role in ("user", "assistant")
    ]
    case["turns"][1]["history_before"] = [
        {"role": "user", "content": "More filler"},
        {"role": "assistant", "content": "Acknowledged"},
    ]
    snapshots = []
    original = SupportService.close

    def close(service):
        # Thread id is available from the benchmark trace, with a real persisted checkpoint.
        snapshots.append(service.graph.get_state(service._config("C100", service.trace.thread_id)).values)
        original(service)

    monkeypatch.setattr(SupportService, "close", close)
    report = run_cases([case], tmp_path, variant=variant)
    assert report["passed"] == 1, report["results"]
    messages = snapshots[0]["messages"]
    assert [m.content for m in messages[:48]] == [m["content"] for m in case["history"]]
    assert len(messages) > 50
    assert any(m.content == "More filler" for m in messages)
    assert report["results"][0]["input_tokens"] is None
    assert report["results"][0]["final_task_projection"] is not None if variant != "B1" else True


def test_padding_compressed_for_model_but_preserved_in_tool_evidence(development, tmp_path):
    case = deepcopy(development[1])
    case["tool_fixture"] = {"padding_tool": "get_order", "padding_chars": 20000}
    report = run_cases([case], tmp_path, variant="M")
    assert report["passed"] == 1, report["results"]
    events = trace_events(report)
    result = next(e for e in events if e["event"] == "tool.execution_end")
    assert len(json.loads(result["result"])["_fixture_padding"]) == 20000
    assert any(e["event"] == "context.built" and e["diagnostics"]["compressed_tool_results"] for e in events)
    assert any(e["event"] == "model.request" and "_context_compressed" in json.dumps(e["messages"]) for e in events)


def test_one_time_read_timeout_safe_failure_then_next_case_passes(development, tmp_path):
    case = deepcopy(development[1])
    case["tool_fixture"] = {"fail_once_tool": "get_order"}
    case["expected"] = {"kind": "safe_failure", "error_code": "tool_timeout"}
    report = run_cases([case, development[1]], tmp_path, variant="B1")
    assert report["passed"] == 2, report["results"]
    assert report["results"][0]["status"] == "safe_failure"
    assert report["unsafe_side_effects"] == 0
    assert sum(e["event"] == "tool.execution_error" for e in trace_events(report)) == 1
    state = next(e for e in trace_events(report) if e["event"] == "task.state")
    assert state["business_before"] == state["business_after"]


@pytest.mark.parametrize("name", ["request_refund", "cancel_order", "escalate_to_human", "select_skill"])
def test_mutation_or_lifecycle_fault_injection_forbidden(name):
    with pytest.raises(ValidationError):
        ToolFixture(fail_once_tool=name)


@pytest.mark.parametrize("variant", ["B1", "M", "MS"])
@pytest.mark.parametrize("point", ["before", "after"])
@pytest.mark.parametrize("approved", [True, False])
def test_restart_resume_fault_recovers_once_without_duplicate_effects(development, tmp_path, variant, point, approved):
    case = deepcopy(development[3 if approved else 4])
    case["approval_script"][0].update(restart=True, failure_point=point)
    report = run_cases([case, development[1]], tmp_path, variant=variant)
    assert report["passed"] == 2, report["results"]
    row = report["results"][0]
    assert row["resume_success"] and row["resume_attempts"] == 2 and row["service_restart_count"] == 1
    assert row["unsafe_side_effects"] == 0
    events = trace_events(report)
    assert (
        next(e for e in events if e["event"] == "fixture.service_restarted")["scope"] == "same_process_service_restart"
    )
    state = next(e for e in events if e["event"] == "task.state")
    assert len(state["business_after"]["refunds"]) == int(approved)


def test_fixture_overrides_isolated_and_invalid_fixture_continues(development, tmp_path):
    custom = deepcopy(development[1])
    custom["fixture_overrides"] = {"1005": {"item": "Isolated fixture item", "total": "6.00"}}
    bad = deepcopy(custom)
    bad["fixture_overrides"] = {"missing": {"item": "Bad"}}
    report = run_cases([bad, custom, development[1]], tmp_path, variant="B1")
    assert [row["task_success"] for row in report["results"]] == [False, True, True]
    assert "Isolated fixture item" in report["results"][1]["final_reply"]
    assert "Isolated fixture item" not in report["results"][2]["final_reply"]


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "user", "content": "unpaired"}],
        [{"role": "tool", "content": "fake"}],
        [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}],
    ],
)
def test_history_cannot_inject_tool_facts_or_partial_pairs(development, history):
    case = deepcopy(development[1])
    case["history"] = history
    with pytest.raises(ValidationError):
        EvalCase.model_validate(case)


def test_unused_approval_script_cannot_make_safe_failure_pass(development, tmp_path):
    case = deepcopy(development[1])
    case.update(
        tool_fixture={"fail_once_tool": "get_order"},
        expected={"kind": "safe_failure", "error_code": "tool_timeout"},
        approval_script=[{"approved": True, "reviewer": "unused"}],
    )
    report = run_cases([case], tmp_path, variant="B1")
    assert not report["results"][0]["task_success"]
    assert "unused_approval_script" in report["results"][0]["failure_reason"]


def test_exception_preserves_business_snapshot_and_continues(development, tmp_path, monkeypatch):
    original = SupportService.chat
    attempts = 0

    def broken_once(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Synthetic unhandled failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SupportService, "chat", broken_once)
    report = run_cases([development[1], development[1]], tmp_path, variant="B1")
    assert [row["task_success"] for row in report["results"]] == [False, True]
    assert any(e["event"] == "task.state" for e in trace_events(report))
    assert report["results"][0]["unsafe_side_effects"] == 0


def test_planned_metric_denominators_include_model_and_setup_failure(development, tmp_path):
    from conftest import ScriptedToolModel
    from langchain_core.messages import AIMessage

    case = deepcopy(development[3])
    case["expected"]["memory"] = {"order_id": "1001"}
    report = run_cases(
        [case], tmp_path, variant="B1", model_factory=lambda: ScriptedToolModel(replies=[AIMessage("Done")])
    )
    row = report["results"][0]
    assert row["resume_success_applicable"] and row["resume_success"] is False
    assert row["memory_retention_applicable"] and row["memory_retention"] is False
    assert report["resume_success_applicable_tasks"] == report["memory_retention_applicable_tasks"] == 1

    def unavailable():
        raise RuntimeError("Synthetic setup failure")

    report = run_cases([case], tmp_path, variant="B1", model_factory=unavailable)
    assert report["resume_success_rate"] == report["memory_retention_rate"] == 0
    assert report["resume_success_applicable_tasks"] == report["memory_retention_applicable_tasks"] == 1


def test_early_safe_stop_does_not_satisfy_unexecuted_later_turns(development, tmp_path):
    case = deepcopy(development[0])
    case.update(
        tool_fixture={"fail_once_tool": "get_order"}, expected={"kind": "safe_failure", "error_code": "tool_timeout"}
    )
    report = run_cases([case], tmp_path, variant="B1")
    assert not report["results"][0]["task_success"]
    assert "unexecuted_turns" in report["results"][0]["failure_reason"]


def test_unsafe_refund_detects_wrong_amount_missing_approval_and_duplicates(development):
    from datetime import date
    from decimal import Decimal

    case = EvalCase.model_validate(development[3])
    store = Store.load(default_data_dir() / "store.json", today=date(2025, 10, 1))
    before = snapshot(store)
    store.record_refund(store.orders["1001"], Decimal("89.00"), "fixture", "fixture-reviewer")
    after = snapshot(store)
    approval = {"approved": True, "reviewer": "fixture-reviewer", "order_id": "1001", "amount": "89.00"}
    assert unsafe_effects(case, before, after, [approval])["unsafe_side_effects"] == 0
    assert "unauthorized_refund" in unsafe_effects(case, before, after, [])["unsafe_effect_reasons"]
    wrong = deepcopy(after)
    wrong["refunds"][0]["amount"] = "90.00"
    assert unsafe_effects(case, before, wrong, [approval])["unsafe_side_effects"] > 0
    duplicate = deepcopy(after)
    duplicate["refunds"].append(deepcopy(after["refunds"][0]))
    assert "duplicate_refund" in unsafe_effects(case, before, duplicate, [approval])["unsafe_effect_reasons"]


def test_state_capture_failure_preserves_business_evidence_and_closes(development, tmp_path, monkeypatch):
    original_close = SupportService.close
    original_chat = SupportService.chat
    closed = []
    attempts = 0

    def close(service):
        closed.append(service)
        original_close(service)

    def chat(service, *args, **kwargs):
        nonlocal attempts
        answer = original_chat(service, *args, **kwargs)
        attempts += 1
        if attempts == 1:

            def broken_state(*args, **kwargs):
                raise RuntimeError("Synthetic checkpoint inspection failure")

            service.graph.get_state = broken_state
        return answer

    monkeypatch.setattr(SupportService, "close", close)
    monkeypatch.setattr(SupportService, "chat", chat)
    report = run_cases([development[1], development[1]], tmp_path, variant="B1")
    assert len(closed) == 2
    assert [row["task_success"] for row in report["results"]] == [False, True]
    assert report["results"][0]["failure_reason"] == "evidence_capture_error"
    assert report["results"][0]["unsafe_side_effects"] == 0
    assert any(event["event"] == "task.state" for event in trace_events(report))
