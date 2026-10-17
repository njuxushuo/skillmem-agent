import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import ScriptedToolModel, call
from langchain_core.messages import AIMessage

from support_agent.evaluation import EvalCase, judge, run_cases, snapshot, sop_checks
from support_agent.models import get_model
from support_agent.service import SupportService, default_data_dir
from support_agent.store import Store
from support_agent.tracing import TraceRecorder, private_directory


@pytest.fixture
def cases():
    return json.loads(Path("data/eval_tasks.json").read_text())


def events(summary, index=0):
    return [json.loads(line) for line in Path(summary["run_dir"], f"1-{index}.jsonl").read_text().splitlines()]


def test_runner_isolation_multiturn_approvals_outputs(cases, tmp_path):
    summary = run_cases(cases, tmp_path, repeat=2)
    assert summary["passed"] == summary["tasks"] == 14
    rows = summary["results"]
    assert len({r["thread_id"] for r in rows}) == 14
    assert all(r["input_tokens"] is None for r in rows)
    assert all(r["unknown_input_tokens_calls"] == r["model_calls"] for r in rows)
    assert rows[3]["tool_proposed_count"] == 2 and rows[3]["tool_executed_count"] == 3
    assert rows[3]["tool_paused_count"] == 1 and rows[3]["tool_success_count"] == 2
    assert rows[4]["tool_rejected_count"] == 1
    trace = events(summary, 3)
    starts = [e for e in trace if e["event"] == "tool.execution_start"]
    assert starts[-2]["tool_call_id"] == starts[-1]["tool_call_id"]
    assert starts[-2]["invocation_id"] != starts[-1]["invocation_id"]
    assert any(e["event"] == "graph.interrupt" for e in trace)
    assert any(e["event"] == "graph.resume" for e in trace)
    state = next(e for e in trace if e["event"] == "task.state")
    assert state["business_before"]["refunds"] == []
    assert len(state["business_after"]["refunds"]) == 1
    assert state["business_after"]["refunds"][0]["approved_by"] == "fixture-reviewer"
    for index in range(7):
        assert next(e for e in events(summary, index) if e["event"] == "task.start")["business_before"]["refunds"] == []
    assert len([e for e in events(summary) if e["event"] == "action.user"]) == 2
    run_dir = Path(summary["run_dir"])
    assert (run_dir / "summary.json").exists() and (run_dir / "results.csv").exists()
    assert len((run_dir / "results.jsonl").read_text().splitlines()) == 14
    assert not list(run_dir.rglob("*.sqlite"))


def test_failure_continues_and_no_fake_completion(cases, tmp_path):
    models = iter([ScriptedToolModel(replies=[AIMessage("Refunded 18.00 EUR for order 1005")]), get_model("offline")])
    report = run_cases([cases[2], cases[5]], tmp_path, model_factory=lambda: next(models), usage_source="mock")
    assert [r["task_success"] for r in report["results"]] == [False, True]
    assert "refund_count" in report["results"][0]["failure_reason"]
    assert report["results"][0]["tool_executed_count"] == 0


def test_invalid_case_and_model_failure_continue(cases, tmp_path):
    attempt = 0

    def factory():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError("model init failed")
        return get_model("offline")

    report = run_cases([{}, cases[0], cases[5]], tmp_path, model_factory=factory)
    assert [r["status"] for r in report["results"]] == ["error", "error", "passed"]
    assert report["results"][0]["failure_reason"] == "ValidationError"
    assert report["results"][1]["input_tokens"] is None


@pytest.mark.parametrize(
    "approval_script",
    [[], [{"approved": True, "reviewer": "fixture-reviewer"}, {"approved": True, "reviewer": "unused"}]],
)
def test_pending_or_unused_approval_is_incomplete(cases, tmp_path, approval_script):
    case = deepcopy(cases[3])
    case["approval_script"] = approval_script
    report = run_cases([case], tmp_path)
    assert not report["results"][0]["task_success"]
    assert "incomplete" in report["results"][0]["failure_reason"]


@pytest.mark.parametrize(
    "tool_call,executed",
    [(call("does_not_exist"), 0), (call("request_refund"), 0), (call("cancel_order", order_id="1002"), 1)],
)
def test_proposal_validation_and_business_rejection(cases, tmp_path, tool_call, executed):
    model = ScriptedToolModel(replies=[tool_call, AIMessage("Cannot complete")])
    report = run_cases([cases[5]], tmp_path, model_factory=lambda: model, usage_source="mock")
    row = report["results"][0]
    assert row["tool_proposed_count"] == 1 and row["tool_executed_count"] == executed
    trace = events(report)
    observations = [e for e in trace if e["event"] == "tool.observation"]
    assert len(observations) == 1
    assert observations[0]["tool_call_id"] == next(e["tool_call_id"] for e in trace if e["event"] == "tool.proposed")
    assert not row["task_success"]
    if executed:
        assert row["tool_rejected_count"] == 1 and row["tool_success_count"] == 0


def test_function_exception_recorded_and_remaining_case_runs(cases, tmp_path, monkeypatch):
    def fail(self, order):
        raise RuntimeError("controlled store failure")

    monkeypatch.setattr(Store, "cancel", fail)
    models = iter(
        [ScriptedToolModel(replies=[call("cancel_order", order_id="1004"), AIMessage("Failed")]), get_model("offline")]
    )
    report = run_cases([cases[5], cases[1]], tmp_path, model_factory=lambda: next(models), usage_source="mock")
    assert not report["results"][0]["task_success"] and report["results"][1]["task_success"]
    assert any(e["event"] == "tool.execution_error" for e in events(report))
    assert report["results"][0]["tool_executed_count"] == 1
    state = next(e for e in events(report) if e["event"] == "task.state")
    assert state["business_before"] == state["business_after"]


def test_loop_limit_still_original_fallback(cases, tmp_path):
    model = ScriptedToolModel(replies=[call("get_order", order_id="1005")])
    report = run_cases([cases[1]], tmp_path, model_factory=lambda: model)
    row = report["results"][0]
    assert not row["task_success"] and row["termination_reason"] == "GraphRecursionError"
    state = next(e for e in events(report) if e["event"] == "task.state")
    assert len(state["business_after"]["tickets"]) == 1


@pytest.mark.parametrize("kind", ["query", "refund", "cancel", "refusal"])
def test_oracle_positive_and_corrupt_outcomes(cases, kind):
    case = EvalCase.model_validate(next(c for c in cases if c["expected"]["kind"] == kind))
    store = Store.load(default_data_dir() / "store.json")
    before = snapshot(store)
    order = store.orders[case.expected.order_id]
    if kind == "refund":
        store.record_refund(order, Decimal("18.00"), "fixture", "policy")
    elif kind == "cancel":
        store.cancel(order)
    after = snapshot(store)
    reply = " ".join(
        [case.expected.order_id, *(str(getattr(order, f)) for f in case.expected.facts), *case.expected.reply_contains]
    )

    def verdict(state=after, text=reply, complete=True):
        return judge(case, before, state, text, completed=complete, approvals=[])["task_success"]

    assert verdict()
    assert not verdict(complete=False)
    assert not verdict(text="done")
    corrupt = deepcopy(after)
    corrupt["orders"]["2001"]["status"] = "cancelled"
    assert not verdict(corrupt)
    if kind == "refund":
        for field, value in [("order_id", "1001"), ("amount", "19.00"), ("approved_by", "invented")]:
            corrupt = deepcopy(after)
            corrupt["refunds"][0][field] = value
            assert not verdict(corrupt)
        assert not verdict(before)
    elif kind == "cancel":
        assert not verdict(before)
    elif kind == "refusal":
        corrupt = deepcopy(after)
        corrupt["orders"][case.expected.order_id]["refunded"] = "89.00"
        assert not verdict(corrupt)


def test_refund_approval_oracle_requires_matching_evidence(cases):
    case = EvalCase.model_validate(cases[3])
    store = Store.load(default_data_dir() / "store.json")
    before = snapshot(store)
    store.record_refund(store.orders["1001"], Decimal("89.00"), "fixture", "fixture-reviewer")
    reply = "Refunded 89.00 EUR for order 1001"

    def check(approvals):
        return judge(case, before, snapshot(store), reply, completed=True, approvals=approvals)["task_success"]

    approval = dict(approved=True, reviewer="fixture-reviewer", order_id="1001", amount="89.00")
    assert check([approval])
    assert not check([])
    assert not check([{**approval, "approved": False}])
    assert not check([{**approval, "order_id": "1005"}])
    assert not check([{**approval, "amount": "18.00"}])


def test_trace_secrets_paths_and_write_failure_do_not_replay(tmp_path):
    with pytest.raises(ValueError, match=".local"):
        private_directory(Path("artifacts/raw"))
    trace = TraceRecorder(
        tmp_path / "t.jsonl", run_id="r", thread_id="t", source="mock", secrets=("test-secret-unique",)
    )
    trace.emit("probe", api_key="hidden", text="Bearer hidden test-secret-unique", nested={"authorization": "hidden"})
    text = trace.path.read_text()
    assert "hidden" not in text and "test-secret-unique" not in text
    assert trace.path.stat().st_mode & 0o077 == 0
    trace.close()
    # Closed sink must not stop a business tool or trigger its retry.
    service = SupportService(trace=trace)
    service.chat("C100", "Refund order 1005", "t")
    assert len(service.store.refunds) == 1 and trace.write_errors


def test_instrumented_and_uninstrumented_business_equivalence():
    # This regression intentionally retains B0's direct-refund-without-eligibility behavior.
    replies = [call("request_refund", order_id="1005", amount=18, reason="fixture"), AIMessage("Done")]
    services = [SupportService(model=ScriptedToolModel(replies=replies)) for _ in range(2)]
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as temp:
        trace = TraceRecorder(Path(temp) / "t.jsonl", run_id="r", thread_id="t", source="mock")
        services[1] = SupportService(model=ScriptedToolModel(replies=replies), trace=trace)
        try:
            answers = [s.chat("C100", "Refund 18", "t") for s in services]
            assert answers[0] == answers[1]
            assert snapshot(services[0].store) == snapshot(services[1].store)
            assert sop_checks(trace.events)["sop_compliance"] is False
        finally:
            trace.close()


def test_new_cli_failure_exit(cases, tmp_path, monkeypatch):
    from support_agent.evaluate import main

    case = deepcopy(cases[5])
    case["expected"]["reply_contains"] = ["impossible fixture text"]
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([case]))
    monkeypatch.setattr("sys.argv", ["evaluate", "--tasks", str(tasks), "--output", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def test_trace_sink_failure_invalidates_measurement(cases, tmp_path, monkeypatch):
    original = TraceRecorder.emit

    def emit(self, event, **fields):
        if event == "task.start":
            self.close()
        original(self, event, **fields)

    monkeypatch.setattr(TraceRecorder, "emit", emit)
    report = run_cases([cases[2]], tmp_path)
    row = report["results"][0]
    assert not row["task_success"] and row["failure_reason"] == "trace_write_error"
    assert row["tool_executed_count"] == 2  # observation failure does not retry the refund


@pytest.mark.parametrize("payload", ["not-json", "[]", "null", "true", "{}", None])
def test_sop_invalid_result_is_not_evidence(payload):
    observed = {"event": "tool.execution_end", "tool_name": "check_refund_eligibility", "result": payload}
    proposal = {"event": "tool.proposed", "tool_name": "request_refund", "args": {"order_id": "1005"}}
    result = sop_checks([observed, proposal])
    assert result["sop_compliance"] is False
    assert result["sop_invalid_result_count"] == 1


@pytest.mark.parametrize(
    "counts,expected",
    [
        ([(0, 0, 0)], "not_run"),
        ([(1, 0, 1)], "failed"),
        ([(2, 1, 1)], "partial_failure"),
        ([(1, 1, 0), (0, 0, 0)], "partial_failure"),
        ([(2, 2, 0)], "api_succeeded"),
    ],
)
def test_real_api_status_requires_responses(counts, expected):
    from support_agent.evaluation import deepseek_api_status

    results = [
        dict(model_calls=calls, model_response_count=responses, model_error_count=errors)
        for calls, responses, errors in counts
    ]
    assert deepseek_api_status(results, "deepseek", "provider") == expected
    assert deepseek_api_status(results, "deepseek", "mock") == "not_run"
    assert deepseek_api_status(results, "offline", "offline") == "not_run"


@pytest.mark.parametrize(
    "reply,fact,matched",
    [
        ("Expected October 3, 2025", "2025-10-03", True),
        ("Expected 3 Oct 2025", "2025-10-03", True),
        ("Expected October 4, 2025", "2025-10-03", False),
        ("Expected February 30, 2025", "2025-02-28", False),
        ("Refunded €18.00", "EUR", True),
        ("Refunded 18.00 EUR", "EUR", True),
        ("Refunded $18.00 USD", "EUR", False),
    ],
)
def test_equivalent_reply_facts_and_wrong_values(reply, fact, matched):
    from support_agent.evaluation import reply_fact_present

    assert reply_fact_present(reply, fact) is matched
