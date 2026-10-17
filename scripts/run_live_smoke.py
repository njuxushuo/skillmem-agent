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

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        with self.edit() as state:
            if state["limits"] != LIMITS:
                raise BudgetStopped("ledger_limits_changed")

    @contextmanager
    def edit(self):
        with self._lock, os.fdopen(os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600), "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"limits": LIMITS, "attempts": [], "stopped": None, "claimed": False}
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
            if state["stopped"] or len(state["attempts"]) >= REQUEST_LIMIT or committed + RESERVATION > USD_LIMIT:
                raise BudgetStopped("request_or_cost_limit_reached")
            index = len(state["attempts"])
            state["attempts"].append(
                {
                    "number": index + 1,
                    "started_at": datetime.now(UTC).isoformat(),
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "upper_usd": str(RESERVATION),
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
                if prompt <= CONTEXT_LIMIT and completion <= OUTPUT_LIMIT and total == prompt + completion:
                    row["upper_usd"] = str((prompt * INPUT_RATE + completion * OUTPUT_RATE) / 1_000_000)
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
            or not 0 < payload["max_tokens"] <= OUTPUT_LIMIT
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Explicitly run the authorized paid smoke")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("No API call: --execute required")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
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
