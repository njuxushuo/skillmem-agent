"""Isolated B0-compatible task runner and explicit business postcondition oracles."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from support_agent.models import get_model
from support_agent.service import SupportService, default_data_dir
from support_agent.store import Store
from support_agent.tracing import TraceRecorder, private_directory


class Turn(BaseModel):
    role: Literal["user"] = "user"
    content: str = Field(min_length=1)


class Approval(BaseModel):
    approved: bool
    reviewer: str = Field(min_length=1)
    note: str | None = None


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["query", "refund", "cancel", "refusal"]
    order_id: str
    # Exact fixture fields that must be communicated; not a prescribed tool sequence.
    facts: list[Literal["status", "tracking", "eta", "item", "total", "currency"]] = Field(default_factory=list)
    reply_contains: list[str] = Field(default_factory=list)
    amount: Decimal | None = None
    approved_by: str | None = None
    requires_approval: bool = False


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    category: str
    customer_id: str
    fixture: Literal["store.json"] = "store.json"
    clock: date
    turns: list[Turn] = Field(min_length=1)
    approval_script: list[Approval] = Field(default_factory=list)
    expected: Expected


def snapshot(store: Store) -> dict:
    """Copy business facts only; no locks/connections or mutable references."""
    return json.loads(
        json.dumps(
            {
                "orders": {k: asdict(v) for k, v in store.orders.items()},
                "refunds": [asdict(r) for r in store.refunds],
                "tickets": store.tickets,
            },
            default=str,
        )
    )


def reply_fact_present(reply: str, fact: str) -> bool:
    """Accept explicit EUR symbols and equivalent English dates; no semantic guessing."""
    if fact.upper() == "EUR":
        return bool(re.search(r"\bEUR\b|€", reply, re.I))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fact):
        months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        month_index = {name.lower(): i for i, month in enumerate(months, 1) for name in (month, month[:3])}
        names = "|".join(month_index)

        def normalize(match):
            try:
                return date(int(match["year"]), month_index[match["month"].lower()], int(match["day"])).isoformat()
            except ValueError:
                return match[0]  # impossible dates are not equivalent evidence

        for pattern in (
            rf"\b(?P<month>{names}) (?P<day>\d{{1,2}}),? (?P<year>\d{{4}})\b",
            rf"\b(?P<day>\d{{1,2}}) (?P<month>{names}) (?P<year>\d{{4}})\b",
        ):
            reply = re.sub(pattern, normalize, reply, flags=re.I)
    return fact.lower() in reply.lower()


def judge(case: EvalCase, before: dict, after: dict, reply: str, *, completed: bool, approvals: list[dict]) -> dict:
    """Compare whole business state, accepting only the fixture's intended mutation."""
    expected = case.expected
    failures = []
    target = deepcopy(before)
    order = before["orders"].get(expected.order_id)
    if not completed:
        failures.append("conversation_incomplete")
    if order is None or order["customer_id"] != case.customer_id:
        return {"task_success": False, "failure_reason": "invalid_oracle_order"}
    if expected.kind == "query":
        if not expected.facts:
            failures.append("query_requires_facts")
        for field in expected.facts:
            fact = order.get(field)
            if fact is None or not reply_fact_present(reply, str(fact)):
                failures.append(f"missing_fact:{field}")
        if expected.order_id not in reply:
            failures.append("missing_order_reference")
    elif expected.kind == "refund":
        refunds = after["refunds"][len(before["refunds"]) :]
        if expected.amount is None or expected.approved_by is None:
            failures.append("invalid_refund_expectation")
        elif len(refunds) != 1:
            failures.append("refund_count")
        else:
            refund = refunds[0]
            if (
                refund["order_id"] != expected.order_id
                or Decimal(refund["amount"]) != expected.amount
                or refund["approved_by"] != expected.approved_by
            ):
                failures.append("refund_facts")
            target["refunds"].append(refund)
            target["orders"][expected.order_id]["refunded"] = str(expected.amount.quantize(Decimal("0.01")))
        if expected.requires_approval and not any(
            a["approved"]
            and a["reviewer"] == expected.approved_by
            and a["order_id"] == expected.order_id
            and Decimal(a["amount"]) == expected.amount
            for a in approvals
        ):
            failures.append("missing_matching_approval")
    elif expected.kind == "cancel":
        target["orders"][expected.order_id]["status"] = "cancelled"
    elif not expected.reply_contains:
        failures.append("refusal_requires_reason")
    if after != target:
        failures.append("unexpected_business_state")
    for text in expected.reply_contains:
        if not reply_fact_present(reply, text):
            failures.append("missing_reply_fact")
    return {"task_success": not failures, "failure_reason": ";".join(failures) or None}


def sop_checks(events: list[dict]) -> dict:
    """One measured soft SOP: successful eligibility check before each refund proposal."""
    eligible = set()
    checks = []
    invalid_results = 0
    for event in events:
        if event["event"] == "tool.execution_end" and event["tool_name"] == "check_refund_eligibility":
            try:
                result = json.loads(event["result"])
            except (TypeError, ValueError):
                result = None
            if not isinstance(result, dict) or not isinstance(result.get("order_id"), str):
                invalid_results += 1
                continue
            if result.get("eligible") is True:
                eligible.add(result["order_id"])
        if event["event"] == "tool.proposed" and event["tool_name"] == "request_refund":
            checks.append(isinstance(event["args"], dict) and event["args"].get("order_id") in eligible)
    return {
        "sop_compliance": all(checks) if checks else None,
        "sop_invalid_result_count": invalid_results,
        "sop_rules": {"eligibility_before_refund": {"applicable": len(checks), "passed": sum(checks)}},
    }


def deepseek_api_status(results: list[dict], provider: str, source: str) -> str:
    """API responses are evidence, not a provider label; independent of business success."""
    if provider != "deepseek" or source != "provider" or not any(r["model_calls"] for r in results):
        return "not_run"
    responses = sum(r["model_response_count"] for r in results)
    if not responses:
        return "failed"
    if any(
        r["model_error_count"] or r["model_calls"] != r["model_response_count"] or not r["model_calls"] for r in results
    ):
        return "partial_failure"
    return "api_succeeded"


def failure_category(result: dict, events: list[dict]) -> str | None:
    if result["task_success"]:
        return None
    errors = [e for e in events if e["event"] == "model.error"]
    if errors:
        return errors[-1].get("failure_category", "api_error")
    if not result["model_calls"]:
        return "setup_error"
    if (
        result["termination_reason"] == "GraphRecursionError"
        or result["tool_protocol_error_count"]
        or any(e["event"] == "tool.proposed" and not e.get("protocol_valid", True) for e in events)
    ):
        return "model_decision_failure"
    return "oracle_failure"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def model_metadata(model) -> dict:
    """Strict allowlist: never serialize a model, clients, headers or credentials."""
    from urllib.parse import urlsplit

    url = urlsplit(str(getattr(model, "openai_api_base", "") or ""))
    return {
        "type": model._llm_type,
        "model": getattr(model, "model_name", None),
        "base_url": f"{url.scheme}://{url.hostname}{url.path}" if url.hostname else None,
        "temperature": getattr(model, "temperature", None),
        "max_tokens": getattr(model, "max_tokens", None),
        "timeout": getattr(model, "request_timeout", None),
        "retries": getattr(model, "max_retries", None),
        "extra_body": getattr(model, "extra_body", None),
    }


def provenance(data_dir: Path, cases: list) -> dict:
    root = Path(__file__).resolve().parents[2]

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    try:
        sha, dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        sha, dirty = None, None
    files = sorted((root / "src/support_agent").glob("*.py"))
    return {
        "git_sha": sha,
        "working_tree_dirty": dirty,
        "source_hash": digest({p.name: p.read_text() for p in files}),
        "dataset_hash": digest(cases),
        "fixture_hash": hashlib.sha256((data_dir / "store.json").read_bytes()).hexdigest(),
        "policy_hash": hashlib.sha256((data_dir / "policies.md").read_bytes()).hexdigest(),
    }


def run_cases(
    cases: list[dict],
    output_dir: Path,
    *,
    provider: str = "offline",
    model_factory=None,
    usage_source: Literal["offline", "mock", "provider"] | None = None,
    repeat: int = 1,
) -> dict:
    """Run every case independently; return public-shaped metrics, keep raw evidence local."""
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a nonempty list")
    if repeat < 1:
        raise ValueError("repeat must be positive")
    output_dir = private_directory(output_dir)
    run_id = uuid.uuid4().hex
    run_dir = private_directory(output_dir / run_id)
    data_dir = default_data_dir()
    meta = provenance(data_dir, cases)
    source = usage_source or ("offline" if provider == "offline" else "provider")
    results = []
    for repetition in range(1, repeat + 1):
        for index, raw_case in enumerate(cases):
            thread_id = uuid.uuid4().hex
            trace = TraceRecorder(
                run_dir / f"{repetition}-{index}.jsonl", run_id=run_id, thread_id=thread_id, source=source
            )
            start = perf_counter()
            result = {
                **meta,
                "case_id": f"invalid_{index}",
                "variant": "B0-compatible",
                "repeat": repetition,
                "thread_id": thread_id,
                "status": "error",
                "task_success": False,
                "failure_reason": None,
                "model_config_hash": None,
                "sop_compliance": None,
            }
            service = None
            try:
                case = EvalCase.model_validate(raw_case)
                result["case_id"] = case.case_id
                model = model_factory() if model_factory else get_model(provider)
                key = getattr(model, "openai_api_key", None)
                if key:
                    trace.secrets = (key.get_secret_value(),)
                config = trace.clean(model_metadata(model))
                result["model_config_hash"] = digest(config)
                store = Store.load(data_dir / case.fixture, today=case.clock)
                before = snapshot(store)
                approvals = []
                with TemporaryDirectory(prefix="db-", dir=run_dir) as temp:
                    service = SupportService(
                        model=model, store=store, db_path=str(Path(temp) / "checkpoint.sqlite"), trace=trace
                    )
                    trace.emit(
                        "task.start",
                        case=case.model_dump(mode="json"),
                        model_config=config,
                        provenance=meta,
                        business_before=before,
                    )
                    try:
                        approval_index = 0
                        answer = None
                        for turn in case.turns:
                            trace.emit("action.user", content=turn.content, business_before=snapshot(store))
                            answer = service.chat(case.customer_id, turn.content, thread_id)
                            trace.emit("action.result", result=asdict(answer), business_after=snapshot(store))
                            while answer.status == "awaiting_approval":
                                trace.emit("graph.interrupt", details=answer.approval)
                                if approval_index == len(case.approval_script):
                                    break
                                decision = case.approval_script[approval_index].model_dump()
                                approval_index += 1
                                approvals.append(
                                    {
                                        **decision,
                                        "order_id": answer.approval["order_id"],
                                        "amount": answer.approval["amount"],
                                    }
                                )
                                trace.emit("graph.resume", decision=decision, business_before=snapshot(store))
                                answer = service.resolve_approval(thread_id, **decision)
                                trace.emit("action.result", result=asdict(answer), business_after=snapshot(store))
                            if answer.status == "awaiting_approval":
                                break
                        completed = answer.status == "answered" and approval_index == len(case.approval_script)
                        graph_errors = [e for e in trace.events if e["event"] == "graph.error"]
                        verdict = judge(
                            case,
                            before,
                            snapshot(store),
                            answer.reply,
                            completed=completed and not graph_errors,
                            approvals=approvals,
                        )
                        result.update(verdict)
                        result["status"] = "passed" if verdict["task_success"] else "failed"
                        result["termination_reason"] = (
                            graph_errors[-1]["error"]
                            if graph_errors
                            else "completed"
                            if completed
                            else "incomplete_script_or_approval"
                        )
                    finally:
                        after = snapshot(store)
                        trace.emit(
                            "task.state",
                            business_before=before,
                            business_after=after,
                            changed={
                                k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]
                            },
                            pending=service.pending_approvals(),
                        )
                        # Evaluation owns this connection; no runtime lifecycle/behavior change.
                        service.registry._db.close()
            except Exception as exc:
                result["failure_reason"] = type(exc).__name__
                result["termination_reason"] = "exception"
                trace.emit("task.error", error=type(exc).__name__, detail=str(exc))
            result.update(trace.statistics())
            result.update(sop_checks(trace.events))
            result["failure_category"] = failure_category(result, trace.events)
            result["latency_ms"] = (perf_counter() - start) * 1000
            if trace.write_errors:
                result.update(
                    status="error",
                    task_success=False,
                    failure_reason="trace_write_error",
                    failure_category="measurement_error",
                )
            trace.emit("task.end", result=result, stop_reason=result["termination_reason"])
            if trace.write_errors:
                result.update(
                    status="error",
                    task_success=False,
                    failure_reason="trace_write_error",
                    failure_category="measurement_error",
                )
            trace.close()
            results.append(result)
    summary = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "provider": provider,
        "usage_source": source,
        "variant": "B0-compatible",
        "tasks": len(results),
        "passed": sum(r["task_success"] for r in results),
        "real_deepseek_status": deepseek_api_status(results, provider, source),
        "results": results,
    }
    applicable = [r for r in results if r["sop_compliance"] is not None]
    summary["task_success_rate"] = summary["passed"] / len(results) if results else None
    summary["sop_compliance_rate"] = (
        sum(r["sop_compliance"] for r in applicable) / len(applicable) if applicable else None
    )
    summary["sop_applicable_tasks"] = len(applicable)
    summary["average_tool_executions"] = (
        sum(r["tool_executed_count"] for r in results) / len(results) if results else None
    )
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        summary[field] = (
            sum(r[field] for r in results) if results and all(r[field] is not None for r in results) else None
        )
        summary[f"known_{field}"] = sum(r[f"known_{field}"] for r in results)
    # These files still stay local; publishing requires an explicit redacted subset.
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (run_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results))
    with (run_dir / "results.csv").open("w", newline="") as file:
        fields = [
            "case_id",
            "status",
            "task_success",
            "failure_reason",
            "tool_proposed_count",
            "tool_executed_count",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "latency_ms",
        ]
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    return summary
