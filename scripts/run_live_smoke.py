"""One bounded Stage 1 smoke: real models, isolated fake business data, no automatic retries.

Requires --execute and DEEPSEEK_API_KEY (or --env-file pointing to an ignored local file).
The fixed ledger is deliberately persistent across processes; do not delete it to retry.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import httpx

# Official USD peak prices checked on 2026-10-17. Reserve full context, NOT an estimated tokenizer count.
PRICING_VERIFIED_ON = "2026-10-17"
PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"
CONTEXT_LIMIT = 1_048_576  # rounds the documented 1M UP, including framing/schema overhead
OUTPUT_LIMIT = 1024
INPUT_RATE = Decimal("0.30")  # USD / 1M, all input conservatively priced as cache misses
OUTPUT_RATE = Decimal("1.20")
REQUEST_LIMIT = 15
USD_LIMIT = Decimal("2.00")
RESERVATION = (CONTEXT_LIMIT * INPUT_RATE + OUTPUT_LIMIT * OUTPUT_RATE) / 1_000_000
LIMITS = {
    "requests": REQUEST_LIMIT,
    "usd": str(USD_LIMIT),
    "per_request_usd": str(RESERVATION),
    "context_limit": CONTEXT_LIMIT,
    "output_limit": OUTPUT_LIMIT,
    "input_rate": str(INPUT_RATE),
    "output_rate": str(OUTPUT_RATE),
    "pricing_url": PRICING_URL,
    "pricing_verified_on": PRICING_VERIFIED_ON,
}


class BudgetStopped(RuntimeError):
    failure_category = "budget_limit"


class BudgetLedger:
    """Reserve before sending; fsync/rename and flock preserve reservations across crashes."""

    def __init__(self, path: Path, *, limits: dict | None = None) -> None:
        self.limits = deepcopy(LIMITS if limits is None else limits)
        self.reservation = (
            self.limits["context_limit"] * Decimal(self.limits["input_rate"])
            + self.limits["output_limit"] * Decimal(self.limits["output_rate"])
        ) / 1_000_000
        if (
            type(self.limits["requests"]) is not int
            or not 0 < self.limits["requests"] <= 2000
            or not Decimal("0") < Decimal(self.limits["usd"]) <= Decimal("3")
            or self.limits["context_limit"] < CONTEXT_LIMIT
            or not 0 < self.limits["output_limit"] <= OUTPUT_LIMIT
            or Decimal(self.limits["input_rate"]) < INPUT_RATE
            or Decimal(self.limits["output_rate"]) < OUTPUT_RATE
            or Decimal(self.limits["per_request_usd"]) != self.reservation
        ):
            raise ValueError("unsafe_budget_configuration")
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        with self.edit() as state:
            if state["limits"] != self.limits:
                raise BudgetStopped("ledger_limits_changed")

    @contextmanager
    def edit(self):
        with self._lock, os.fdopen(os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600), "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"limits": self.limits, "attempts": [], "stopped": None, "claimed": False}
            )
            yield state
            temp = self.path.with_suffix(".tmp")
            with os.fdopen(os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w") as file:
                json.dump(state, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def claim_run(self):
        with self.edit() as state:
            if state["claimed"] or state["attempts"]:
                raise BudgetStopped("smoke_already_started; review ledger instead of rerunning")
            state["claimed"] = True

    def reserve(self, payload: bytes) -> int:
        with self.edit() as state:
            committed = sum(Decimal(a["upper_usd"]) for a in state["attempts"])
            if (
                state["stopped"]
                or len(state["attempts"]) >= self.limits["requests"]
                or committed + self.reservation > Decimal(self.limits["usd"])
            ):
                raise BudgetStopped("request_or_cost_limit_reached")
            index = len(state["attempts"])
            state["attempts"].append(
                {
                    "number": index + 1,
                    "started_at": datetime.now(UTC).isoformat(),
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "upper_usd": str(self.reservation),
                    "status": "reserved",
                    "usage": None,
                }
            )
            return index

    def settle(self, index: int, *, http_status=None, usage=None, latency_ms=None, error=None):
        with self.edit() as state:
            row = state["attempts"][index]
            row.update(
                http_status=http_status,
                usage=usage,
                latency_ms=latency_ms,
                status="response" if http_status == 200 else "api_error",
                error=error,
            )
            if http_status != 200:
                state["stopped"] = "api_error"  # no repeat of authentication/rate/network failures
                return
            values = (
                [usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")]
                if isinstance(usage, dict)
                else [None] * 3
            )
            if all(type(v) is int and v >= 0 for v in values):
                prompt, completion, total = values
                if (
                    prompt <= self.limits["context_limit"]
                    and completion <= self.limits["output_limit"]
                    and total == prompt + completion
                ):
                    row["upper_usd"] = str(
                        (prompt * Decimal(self.limits["input_rate"]) + completion * Decimal(self.limits["output_rate"]))
                        / 1_000_000
                    )
                    row["usage_complete"] = True
                else:
                    state["stopped"] = "usage_contract_violation"
            # Missing/invalid usage never releases the reservation, even on HTTP 200.

    def summary(self) -> dict:
        with self.edit() as state:
            return {
                **state,
                "http_attempts": len(state["attempts"]),
                "cost_upper_usd": str(sum(Decimal(a["upper_usd"]) for a in state["attempts"])),
                "actual_billed_usd": None,
            }


class BoundedTransport(httpx.BaseTransport):
    """The only paid HTTP path: cap physical attempts, not callbacks swallowed by LangChain."""

    def __init__(self, ledger: BudgetLedger, inner: httpx.BaseTransport):
        self.ledger, self.inner = ledger, inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if (
            request.method != "POST"
            or str(request.url) != "https://api.deepseek.com/chat/completions"
            or payload.get("model") != "deepseek-flash"
            or payload.get("thinking") != {"type": "disabled"}
            or payload.get("stream") is not False
            or type(payload.get("max_tokens")) is not int
            or not 0 < payload["max_tokens"] <= self.ledger.limits["output_limit"]
            or payload.get("n", 1) != 1
            or "max_completion_tokens" in payload
            or any(t.get("type") != "function" for t in payload.get("tools", []))
        ):
            raise BudgetStopped("request_outside_approved_smoke_configuration")
        index = self.ledger.reserve(request.content)
        start = perf_counter()
        try:
            response = self.inner.handle_request(request)
            response.read()
            try:
                body = response.json()
                usage = body.get("usage") if isinstance(body, dict) else None
            except ValueError:
                usage = None
            self.ledger.settle(
                index, http_status=response.status_code, usage=usage, latency_ms=(perf_counter() - start) * 1000
            )
            return response
        except Exception as exc:
            self.ledger.settle(index, error=type(exc).__name__, latency_ms=(perf_counter() - start) * 1000)
            raise

    def close(self):
        self.inner.close()


def load_key(env_file: Path | None):
    """Read ONLY the one credential setting, without shell execution or printing the file."""
    if not os.environ.get("DEEPSEEK_API_KEY") and env_file:
        for line in env_file.read_text().splitlines():
            name, sep, value = line.removeprefix("export ").partition("=")
            if sep and name.strip() == "DEEPSEEK_API_KEY":
                os.environ["DEEPSEEK_API_KEY"] = value.strip().strip("\"'")
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        raise ValueError("missing_environment_credential")


def audit_smoke(path: Path) -> dict:
    """Rejudge existing real evidence OFFLINE. Never invoke a model or alter original traces."""
    from support_agent.evaluation import EvalCase, judge, provenance
    from support_agent.policies_kb import PolicyBase
    from support_agent.policy import PolicyConfig
    from support_agent.service import default_data_dir
    from support_agent.store import Store
    from support_agent.tools import make_tools

    original = json.loads(path.read_text())
    summary, budget = original["evaluation"], original["budget"]
    data = default_data_dir()
    schemas = {
        t.name: t.args_schema
        for t in make_tools(Store.load(data / "store.json"), PolicyBase(data / "policies.md"), PolicyConfig())
    }
    audited = []
    for index, row in enumerate(summary["results"]):
        events = [json.loads(line) for line in (Path(summary["run_dir"]) / f"1-{index}.jsonl").read_text().splitlines()]
        start = next(e for e in events if e["event"] == "task.start")
        state = next(e for e in events if e["event"] == "task.state")
        reply = [e for e in events if e["event"] == "action.result"][-1]["result"]["reply"]
        approval, decisions = None, []
        for event in events:
            if event["event"] == "graph.interrupt":
                approval = event["details"]
            if event["event"] == "graph.resume":
                decisions.append({**event["decision"], "order_id": approval["order_id"], "amount": approval["amount"]})
        verdict = judge(
            EvalCase.model_validate(start["case"]),
            state["business_before"],
            state["business_after"],
            reply,
            completed=row["termination_reason"] == "completed",
            approvals=decisions,
        )
        protocol_errors = []
        proposals = [e for e in events if e["event"] == "tool.proposed"]
        for proposal in proposals:
            try:
                schemas[proposal["tool_name"]].model_validate(proposal["args"])
                if not proposal["protocol_valid"]:
                    raise ValueError("invalid_call")
            except (KeyError, ValueError, TypeError):
                protocol_errors.append("invalid_tool_or_arguments")
        ids = [e["tool_call_id"] for e in proposals]
        observations = [e["tool_call_id"] for e in events if e["event"] == "tool.observation"]
        if len(ids) != len(set(ids)) or sorted(ids) != sorted(observations):
            protocol_errors.append("proposal_observation_pairing")
        for event in events:
            if event["event"] != "model.request":
                continue
            for group in event["messages"]:
                pending = set()
                for message in group:
                    body = message["data"]
                    if message["type"] == "tool":
                        call_id = body["tool_call_id"]
                        if call_id not in pending:
                            protocol_errors.append("orphan_tool_message")
                        pending.discard(call_id)
                    else:
                        if pending:
                            protocol_errors.append("unresolved_tool_message")
                        if message["type"] == "ai":
                            pending = {c["id"] for c in body["tool_calls"]}
                if pending:
                    protocol_errors.append("unfinished_model_input")
        usage = [e["usage"]["raw"] for e in events if e["event"] == "model.response"]
        cache_complete = all(
            isinstance(u, dict)
            and all(
                type(u.get(k)) is int
                for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens", "prompt_tokens")
            )
            and u["prompt_cache_hit_tokens"] + u["prompt_cache_miss_tokens"] == u["prompt_tokens"]
            for u in usage
        )
        peak = (
            sum(
                (
                    Decimal("0.006") * u["prompt_cache_hit_tokens"]
                    + INPUT_RATE * u["prompt_cache_miss_tokens"]
                    + OUTPUT_RATE * u["completion_tokens"]
                )
                / 1_000_000
                for u in usage
            )
            if cache_complete
            else None
        )
        audited.append(
            {
                "case_id": row["case_id"],
                "original_task_success": row["task_success"],
                "original_failure_reason": row["failure_reason"],
                "rejudged": verdict,
                "protocol_errors": protocol_errors,
                "approval_resume_seen": bool(decisions),
                "reply": reply,
                "peak_price_calculated_usd": str(peak) if peak is not None else None,
                "off_peak_price_calculated_usd": str(peak / 2) if peak is not None else None,
            }
        )
    passed = (
        summary["usage_source"] == "provider"
        and summary["real_deepseek_status"] == "api_succeeded"
        and len(audited) == 3
        and all(r["rejudged"]["task_success"] and not r["protocol_errors"] for r in audited)
        and all(r["total_tokens"] is not None for r in summary["results"])
        and all(r["sop_compliance"] is not False for r in summary["results"])
        and audited[-1]["approval_resume_seen"]
        and budget["http_attempts"] <= REQUEST_LIMIT
        and Decimal(budget["cost_upper_usd"]) <= USD_LIMIT
        and not budget["stopped"]
    )
    audit_source = provenance(data, [])
    return {
        "audit_code_commit": audit_source["git_sha"],
        "audit_source_hash": audit_source["source_hash"],
        "audit_working_tree_dirty": audit_source["working_tree_dirty"],
        "original_result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": "offline_rejudgment_of_saved_trace",
        "new_api_calls": 0,
        "original_code_commit": summary["results"][0]["git_sha"],
        "gate": "PASS" if passed else "FAIL",
        "actual_billed_usd": None,
        "results": audited,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Explicitly run the authorized paid smoke")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--audit-only", action="store_true", help="Offline rejudgment; no model calls")
    args = parser.parse_args()
    if args.audit_only and args.execute:
        raise SystemExit("Choose execute OR offline audit")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    if args.audit_only:
        audit = audit_smoke(root / ".local/stage1_live/live_result.json")
        (root / ".local/stage1_live/audit_result.json").write_text(json.dumps(audit, indent=2) + "\n")
        print(json.dumps(audit))
        return 0 if audit["gate"] == "PASS" else 1
    if not args.execute:
        raise SystemExit("No API call: --execute required")
    os.environ.update(
        SUPPORT_MODEL_PROVIDER="deepseek",
        DEEPSEEK_MODEL="deepseek-flash",
        DEEPSEEK_BASE_URL="https://api.deepseek.com",
        DEEPSEEK_MAX_TOKENS=str(OUTPUT_LIMIT),
        DEEPSEEK_TIMEOUT="60",
        DEEPSEEK_MAX_RETRIES="0",
        DEEPSEEK_TEMPERATURE="0",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        LANGCHAIN_TRACING="false",
    )
    if datetime.now(UTC).date().isoformat() != PRICING_VERIFIED_ON:
        raise ValueError("pricing_review_required_before_new_date")
    load_key(args.env_file)
    from support_agent.evaluation import run_cases
    from support_agent.models import get_model

    directory = root / ".local/stage1_live"
    ledger = BudgetLedger(directory / "budget.json")
    ledger.claim_run()
    # Trust the configured proxy for connectivity, never redirects or HTTP/SDK retries.
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    transport = BoundedTransport(ledger, httpx.HTTPTransport(retries=0, proxy=proxy))
    cases = json.loads((root / "data/eval_tasks.json").read_text())
    cases = [c for c in cases if c["case_id"] in {"shipping_multi_turn", "refund_small", "refund_approved"}]
    # The shipping smoke is the final shipping turn only; no extra order query.
    cases[0]["turns"] = cases[0]["turns"][-1:]
    with httpx.Client(transport=transport, follow_redirects=False, timeout=60) as client:
        summary = run_cases(
            cases,
            directory,
            provider="deepseek",
            variant="B0-compatible",
            usage_source="provider",
            model_factory=lambda: get_model("deepseek", http_client=client),
        )
    result = {"evaluation": summary, "budget": ledger.summary()}
    (directory / "live_result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "tasks_passed": summary["passed"],
                "tasks": summary["tasks"],
                "api_status": summary["real_deepseek_status"],
                "http_attempts": result["budget"]["http_attempts"],
                "cost_upper_usd": result["budget"]["cost_upper_usd"],
                "run_dir": summary["run_dir"],
            }
        )
    )
    return 0 if summary["passed"] == 3 and summary["real_deepseek_status"] == "api_succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
