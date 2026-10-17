"""Skill overhead measured on the actual SDK wire using offline MockTransport."""

import json
from pathlib import Path

import pytest
from test_deepseek import mock_model, response

from support_agent.evaluation import run_cases
from support_agent.skills import load_skills


def test_mock_skill_routing_usage_and_dynamic_schema(monkeypatch, tmp_path):
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    model, requests, client = mock_model(
        monkeypatch,
        [
            response(calls=[("select-1", "select_skill", {"skill_id": "order_shipping"})], usage=usage),
            response(calls=[("order-1", "get_order", {"order_id": "1005"})], usage=usage),
            response("Order 1005: Bottle cage, delivered, 18.00 EUR.", usage=usage),
        ],
    )
    case = json.loads(Path("data/eval_tasks.json").read_text())[1]
    try:
        summary = run_cases(
            [case], tmp_path, provider="deepseek", model_factory=lambda: model, usage_source="mock", variant="MS"
        )
    finally:
        client.close()
    row = summary["results"][0]
    assert row["task_success"], row["failure_reason"]
    assert row["skill_routing_count"] == row["skill_switch_count"] == row["skill_model_calls"] == 1
    assert row["tool_executed_count"] == row["business_tool_proposed_count"] == 1
    assert row["total_tool_executed_count"] == row["tool_proposed_count"] == 2
    assert row["input_tokens"] == 300 and row["skill_input_tokens"] == 100
    assert row["total_tokens"] == 330 and row["skill_total_tokens"] == 110
    assert row["skill_routing_latency_ms"] >= 0
    assert row["full_sop"] is False and summary["real_deepseek_status"] == "not_run"
    assert {t["function"]["name"] for t in requests[0]["tools"]} == {"select_skill", "escalate_to_human"}
    assert {t["function"]["name"] for t in requests[1]["tools"]} == {
        "select_skill",
        "escalate_to_human",
        "list_my_orders",
        "get_order",
        "track_shipment",
    }
    assert any(m.get("tool_call_id") == "select-1" for m in requests[1]["messages"])
    skills = load_skills(Path("data"))
    assert skills["order_shipping"].sop in requests[1]["messages"][0]["content"]
    assert skills["refund"].sop not in requests[1]["messages"][0]["content"]


def test_same_sop_fairness_control_keeps_all_tools_and_no_router(monkeypatch, tmp_path):
    model, requests, client = mock_model(monkeypatch, [response("Order 1005: Bottle cage, delivered, 18.00 EUR.")])
    case = json.loads(Path("data/eval_tasks.json").read_text())[1]
    try:
        summary = run_cases(
            [case],
            tmp_path,
            provider="deepseek",
            model_factory=lambda: model,
            usage_source="mock",
            variant="B1",
            full_sop=True,
        )
    finally:
        client.close()
    row = summary["results"][0]
    assert row["full_sop"] is True and row["skill_routing_count"] == 0
    assert len(requests[0]["tools"]) == 8
    assert "select_skill" not in {t["function"]["name"] for t in requests[0]["tools"]}
    assert all(s.sop in requests[0]["messages"][0]["content"] for s in load_skills(Path("data")).values())


@pytest.mark.parametrize("mixed,missing_usage", [(False, False), (True, False), (False, True)])
def test_invalid_skill_proposals_count_routing_response_once(monkeypatch, tmp_path, mixed, missing_usage):
    usage = None if missing_usage else {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    first = response(calls=[("bad-select", "select_skill", {"skill_id": "refund"})], usage=usage)
    first["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{invalid"
    if mixed:
        first["choices"][0]["message"]["tool_calls"].append(
            {
                "id": "valid-select",
                "type": "function",
                "function": {"name": "select_skill", "arguments": '{"skill_id":"refund"}'},
            }
        )
    model, requests, client = mock_model(monkeypatch, [first, response("Please clarify.", usage=usage)])
    case = json.loads(Path("data/eval_tasks.json").read_text())[1]
    try:
        report = run_cases(
            [case], tmp_path, provider="deepseek", model_factory=lambda: model, usage_source="mock", variant="MS"
        )
    finally:
        client.close()
    row = report["results"][0]
    assert len(requests) == row["model_calls"] == 2
    assert row["skill_model_calls"] == 1  # response-level, not number of proposed calls
    assert row["skill_input_tokens"] == (None if missing_usage else 100)
    assert row["input_tokens"] == (None if missing_usage else 200)
    assert row["skill_routing_count"] == row["skill_switch_count"] == row["tool_executed_count"] == 0
    assert row["guard_rejected_count"] == 1
    assert row["tool_protocol_error_count"] == (2 if mixed else 1)
    ids = {m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"}
    assert ids == ({"bad-select", "valid-select"} if mixed else {"bad-select"})
