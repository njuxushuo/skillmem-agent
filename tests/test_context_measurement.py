"""Context diagnostics remain estimates, separate from provider/mock usage."""

import json
from pathlib import Path

from test_deepseek import mock_model, response

from support_agent.evaluation import run_cases


def test_memory_runner_records_context_and_mock_usage(monkeypatch, tmp_path):
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    model, requests, client = mock_model(
        monkeypatch,
        [
            response(calls=[("order-1", "get_order", {"order_id": "1005"})], usage=usage),
            response("Order 1005: Bottle cage, delivered, 18.00 EUR.", usage=usage),
        ],
    )
    case = json.loads(Path("data/eval_tasks.json").read_text())[1]
    try:
        summary = run_cases(
            [case], tmp_path, provider="deepseek", model_factory=lambda: model, usage_source="mock", variant="M"
        )
    finally:
        client.close()
    row = summary["results"][0]
    assert row["task_success"], row["failure_reason"]
    assert row["context_strategy"] == "structured_memory"
    assert row["context_budget"]["output_tokens"] == 1024
    assert row["execution_limits"]["model_calls"] == 8
    assert row["input_tokens"] == 200
    assert summary["real_deepseek_status"] == "not_run"
    events = [
        json.loads(line)
        for path in Path(summary["run_dir"]).glob("1-*.jsonl")
        for line in path.read_text().splitlines()
    ]
    contexts = [e for e in events if e["event"] == "context.measured"]
    assert len(contexts) == len(requests) == 2
    assert all(e["actual_input_tokens"] == 100 and e["usage_source"] == "mock" for e in contexts)
    assert all(e["diagnostics"]["estimated_input_tokens"] > 100 for e in contexts)
