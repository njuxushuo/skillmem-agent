"""MockTransport tests use the real OpenAI SDK/LangChain/ToolNode stack, never network."""

import json
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai.chat_models.base import BaseChatOpenAI
from openai import APIStatusError, APITimeoutError

from support_agent.evaluation import run_cases
from support_agent.models import get_model
from support_agent.service import SupportService
from support_agent.tracing import TraceRecorder, usage_of


def mock_model(monkeypatch, responses):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        response = responses[len(requests) - 1]
        if isinstance(response, Exception):
            raise response
        return httpx.Response(response.pop("http_status", 200), json=response)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-credential-not-real")
    monkeypatch.setattr(
        "langchain_openai.chat_models.base.BaseChatOpenAI",
        lambda **kwargs: BaseChatOpenAI(http_client=client, **kwargs),
    )
    return get_model("deepseek"), requests, client


def response(content="", calls=None, usage=None):
    result = {
        "id": "mock-response",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
    }
    if calls:
        result["choices"][0]["message"]["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for cid, name, args in calls
        ]
    if usage is not None:
        result["usage"] = usage
    return result


def test_mock_multistep_tool_ids_and_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.invalid/v1")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT", "12")
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "123")
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "prompt_cache_hit_tokens": 70,
        "prompt_cache_miss_tokens": 30,
    }
    model, requests, client = mock_model(
        monkeypatch,
        [
            response(calls=[("elig-1", "check_refund_eligibility", {"order_id": "1005"})], usage=usage),
            response(
                calls=[("pay-1", "request_refund", {"order_id": "1005", "amount": 18, "reason": "broken"})], usage=usage
            ),
            response("Refunded 18.00 EUR for order 1005", usage=usage),
        ],
    )
    cases = json.loads(Path("data/eval_tasks.json").read_text())
    try:
        report = run_cases([cases[2]], tmp_path, provider="deepseek", model_factory=lambda: model, usage_source="mock")
    finally:
        client.close()
    row = report["results"][0]
    assert row["task_success"] and row["sop_compliance"]
    assert row["input_tokens"] == 300 and row["output_tokens"] == 30 and row["total_tokens"] == 330
    assert row["model_calls"] == 3 and row["tool_executed_count"] == 2
    assert report["real_deepseek_status"] == "not_run"
    assert model.request_timeout == 12
    for request in requests:
        assert request["thinking"] == {"type": "disabled"}
        assert request["model"] == "deepseek-flash"
        assert request["max_tokens"] == 123
        assert "max_completion_tokens" not in request
        assert request["temperature"] == 0 and not request["stream"]
        assert len(request["tools"]) == 8
        assert "parallel_tool_calls" not in request
        assert "reasoning_content" not in json.dumps(request)
        assert "customer_id" not in json.dumps([t["function"]["parameters"] for t in request["tools"]])
    tool_results = [m for m in requests[2]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["elig-1", "pay-1"]
    assert json.loads(tool_results[-1]["content"])["status"] == "refunded"
    trace = Path(report["run_dir"], "1-0.jsonl").read_text()
    assert "unit-test-credential-not-real" not in trace
    assert "prompt_cache_hit_tokens" in trace
    rows = [json.loads(line) for line in trace.splitlines()]
    request_event = next(e for e in rows if e["event"] == "model.request")
    assert len(request_event["parameters"]["tools"]) == 8


@pytest.mark.parametrize(
    "wire,expected",
    [
        (None, (None, None, None)),
        ({}, (None, None, None)),
        ({"prompt_tokens": 8}, (8, None, None)),
        ({"prompt_tokens": 8, "completion_tokens": 0, "total_tokens": 8}, (8, 0, 8)),
    ],
)
def test_missing_partial_zero_usage(monkeypatch, tmp_path, wire, expected):
    model, _, client = mock_model(monkeypatch, [response("Hello", usage=wire)])
    trace = TraceRecorder(tmp_path / "trace.jsonl", run_id="r", thread_id="t", source="mock")
    try:
        message = model.invoke("hi", config={"callbacks": [trace]})
        usage = usage_of(message)
        assert tuple(usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")) == expected
        stats = trace.statistics()
        assert tuple(stats[k] for k in ("input_tokens", "output_tokens", "total_tokens")) == expected
    finally:
        client.close()
        trace.close()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_no_retry_or_fake_usage(monkeypatch, tmp_path, status):
    model, requests, client = mock_model(
        monkeypatch, [{"http_status": status, "error": {"message": "mock failure", "type": "mock"}}]
    )
    trace = TraceRecorder(tmp_path / "trace.jsonl", run_id="r", thread_id="t", source="mock")
    try:
        with pytest.raises(APIStatusError):
            model.invoke("hi", config={"callbacks": [trace]})
        assert len(requests) == 1
        assert trace.statistics()["input_tokens"] is None
        assert any(e["event"] == "model.error" for e in trace.events)
    finally:
        client.close()
        trace.close()


def test_timeout_recorded(monkeypatch, tmp_path):
    model, requests, client = mock_model(monkeypatch, [httpx.ReadTimeout("mock timeout")])
    trace = TraceRecorder(tmp_path / "trace.jsonl", run_id="r", thread_id="t", source="mock")
    try:
        with pytest.raises(APITimeoutError, match="timed out"):
            model.invoke("hi", config={"callbacks": [trace]})
        assert len(requests) == 1
        assert trace.statistics()["unknown_input_tokens_calls"] == 1
    finally:
        client.close()
        trace.close()


def test_config_validation(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        get_model("deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-test-value")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT", "nan")
    with pytest.raises(ValueError, match="Invalid DeepSeek"):
        get_model("deepseek")


def test_normalized_only_usage():
    message = AIMessage("ok", usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6})
    assert usage_of(message)["total_tokens"] == 6


def test_parallel_calls_preserve_b0_behavior(monkeypatch, tmp_path):
    model, requests, client = mock_model(
        monkeypatch,
        [
            response(calls=[("a", "get_order", {"order_id": "1005"}), ("b", "track_shipment", {"order_id": "1002"})]),
            response("Done"),
        ],
    )
    trace = TraceRecorder(tmp_path / "trace.jsonl", run_id="r", thread_id="t", source="mock")
    try:
        service = SupportService(model=model, trace=trace)
        service.chat("C100", "Check two orders", "t")
        assert {m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"} == {"a", "b"}
        assert trace.statistics()["tool_executed_count"] == 2
        assert {e["tool_call_id"] for e in trace.events if e["event"] == "tool.execution_start"} == {"a", "b"}
    finally:
        client.close()
        trace.close()


def test_malformed_arguments_observed_without_execution(monkeypatch, tmp_path):
    wire = response(calls=[("bad", "request_refund", {})])
    wire["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "not-json"
    model, _, client = mock_model(monkeypatch, [wire])
    trace = TraceRecorder(tmp_path / "trace.jsonl", run_id="r", thread_id="t", source="mock")
    try:
        SupportService(model=model, trace=trace).chat("C100", "refund 1005", "t")
        assert trace.statistics()["tool_proposed_count"] == 1
        assert trace.statistics()["tool_executed_count"] == 0
        assert not next(e for e in trace.events if e["event"] == "tool.proposed")["protocol_valid"]
    finally:
        client.close()
        trace.close()


def test_mixed_known_unknown_usage_keeps_subtotal(monkeypatch, tmp_path):
    model, _, client = mock_model(
        monkeypatch,
        [
            response(
                calls=[("a", "get_order", {"order_id": "1005"})],
                usage={"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
            ),
            response("Done"),
        ],
    )
    trace = TraceRecorder(tmp_path / "trace.jsonl", run_id="r", thread_id="t", source="mock")
    try:
        SupportService(model=model, trace=trace).chat("C100", "order 1005", "t")
        stats = trace.statistics()
        assert stats["input_tokens"] is None and stats["known_input_tokens"] == 20
        assert stats["unknown_input_tokens_calls"] == 1 and stats["model_calls"] == 2
    finally:
        client.close()
        trace.close()


def test_mock_approval_resume_retains_pending_call_id(monkeypatch, tmp_path):
    model, requests, client = mock_model(
        monkeypatch,
        [
            response(calls=[("elig", "check_refund_eligibility", {"order_id": "1001"})]),
            response(calls=[("refund", "request_refund", {"order_id": "1001", "amount": 89, "reason": "broken"})]),
            response("Refunded 89.00 EUR for order 1001"),
        ],
    )
    case = json.loads(Path("data/eval_tasks.json").read_text())[3]
    try:
        report = run_cases([case], tmp_path, provider="deepseek", model_factory=lambda: model, usage_source="mock")
        assert report["passed"] == 1 and len(requests) == 3
        row = report["results"][0]
        assert row["tool_proposed_count"] == 2 and row["tool_executed_count"] == 3
        assert row["tool_reentry_count"] == 1
        tool_results = [m for m in requests[2]["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_results] == ["elig", "refund"]
        assert json.loads(tool_results[-1]["content"])["approved_by"] == "fixture-reviewer"
    finally:
        client.close()
