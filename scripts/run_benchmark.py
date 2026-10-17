"""Frozen Stage 5 paired experiment. Paid calls require explicit --execute and one shared ledger.

Each phase is single-use. Never delete the ledger or change frozen files to retry a paid experiment.
Raw evidence stays in .local/stage5; public exports contain metrics and trace digests only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median

import httpx
from run_live_smoke import LIMITS, BoundedTransport, BudgetLedger, BudgetStopped, load_key

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / ".local/stage5"
MANIFEST = ROOT / "data/benchmark_manifest.json"
CONFIG = {
    "model": "deepseek-flash",
    "thinking": "disabled",
    "temperature": 0,
    "max_tokens": 1024,
    "timeout_seconds": 60,
    "retries": 0,
    "model_calls_per_turn": 8,
    "tool_calls_per_turn": 8,
    "turn_seconds": 120,
    "tool_seconds": 5,
    "context_strategy": "structured_memory",
    "context_estimated_total": 16384,
    "context_margin": 512,
    "variants": ["B1", "M", "MS", "B1_SOP"],
    "repeat": 3,
    "parallel_cases": 4,
    "pilot_cases": 3,
    "limits": {**LIMITS, "requests": 2000, "usd": "3.00"},
    "pricing_html_sha256": "38f583f627523f45a0cacd00d43ad49cd4f7e3aa5c20cc98bdbc3674f5c74c95",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def frozen_files(root=ROOT) -> list[Path]:
    files = [root / name for name in ("requirements.lock", "pyproject.toml", ".python-version")]
    for pattern in (
        "src/support_agent/*.py",
        "scripts/*.py",
        "tests/*.py",
        "data/*.json",
        "data/*.md",
        "data/skills/*.md",
    ):
        files.extend(root.glob(pattern))
    return sorted(p for p in set(files) if p.name != "benchmark_manifest.json")


def freeze(root=ROOT, directory=DIRECTORY):
    if (directory / "budget.json").exists():
        raise ValueError("Do not refreeze an experiment after its paid ledger exists")
    manifest = {"version": 1, "config": CONFIG, "files": {str(p.relative_to(root)): sha(p) for p in frozen_files(root)}}
    manifest["protocol_hash"] = digest(manifest)
    target = root / "data/benchmark_manifest.json"
    if target.exists():
        raise ValueError("Manifest already exists; review the previous freeze instead of overwriting it")
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_manifest(root=ROOT) -> dict:
    manifest = json.loads((root / "data/benchmark_manifest.json").read_text())
    body = {k: v for k, v in manifest.items() if k != "protocol_hash"}
    current = {str(p.relative_to(root)): sha(p) for p in frozen_files(root)}
    if manifest["protocol_hash"] != digest(body) or manifest["config"] != CONFIG or manifest["files"] != current:
        raise ValueError("frozen_protocol_or_source_changed")
    return manifest


def claim_phase(ledger, phase: str, protocol_hash: str):
    with ledger.edit() as state:
        if state.get("protocol_hash", protocol_hash) != protocol_hash:
            raise BudgetStopped("experiment_protocol_changed")
        phases = state.setdefault("phases", {})
        if phase in phases:
            raise BudgetStopped("phase_already_claimed; preserve partial results, do not rerun")
        if phase == "final" and not phases.get("pilot", {}).get("validated"):
            raise BudgetStopped("pilot_not_validated")
        state["protocol_hash"] = protocol_hash
        state["claimed"] = True
        phases[phase] = {"claimed_at": datetime.now(UTC).isoformat(), "completed": False, "validated": False}


class DatedTransport(BoundedTransport):
    def handle_request(self, request):
        if datetime.now(UTC).date().isoformat() != self.ledger.limits["pricing_verified_on"]:
            raise BudgetStopped("pricing_review_required_for_new_utc_date")
        return super().handle_request(request)


def protocol_errors(events: list[dict]) -> list[str]:
    errors = []
    for event in events:
        if event["event"] != "model.request":
            continue
        for group in event["messages"]:
            pending = set()
            for message in group:
                body = message["data"]
                if message["type"] == "tool":
                    if body["tool_call_id"] not in pending:
                        errors.append("orphan_or_duplicate_result")
                    pending.discard(body["tool_call_id"])
                else:
                    if pending:
                        errors.append("unfinished_tool_group")
                    pending = {c["id"] for c in [*body.get("tool_calls", []), *body.get("invalid_tool_calls", [])]}
            if pending:
                errors.append("unfinished_model_input")
    return errors


def _execute_case(job, client, ledger, phase, manifest):
    from support_agent.context import ContextBudget
    from support_agent.evaluation import run_cases
    from support_agent.execution import ExecutionLimits
    from support_agent.models import get_model

    index, case, label, repetition = job
    state = ledger.summary()
    if state["stopped"] or state["http_attempts"] >= CONFIG["limits"]["requests"]:
        return {
            "case_id": case["case_id"],
            "category": case["category"],
            "experiment_variant": label,
            "repeat": repetition,
            "schedule_index": index,
            "status": "not_run",
            "task_success": False,
            "failure_reason": "global_budget_or_api_stop",
            "phase": phase,
            "protocol_hash": manifest["protocol_hash"],
            "usage_source": "provider",
        }
    variant = "B1" if label == "B1_SOP" else label
    report = run_cases(
        [case],
        DIRECTORY / phase,
        provider="deepseek",
        usage_source="provider",
        variant=variant,
        full_sop=label == "B1_SOP",
        model_factory=lambda: get_model("deepseek", http_client=client),
        limits=ExecutionLimits(8, 8, 120, 5),
        context_budget=ContextBudget(16384, 1024, 512) if variant in {"M", "MS"} else None,
        context_strategy=CONFIG["context_strategy"] if variant in {"M", "MS"} else None,
    )
    row = report["results"][0]
    trace_path = Path(report["run_dir"]) / "1-0.jsonl"
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    row.update(
        experiment_variant=label,
        repeat=repetition,
        phase=phase,
        schedule_index=index,
        protocol_hash=manifest["protocol_hash"],
        raw_trace_path=str(trace_path),
        raw_trace_sha256=sha(trace_path),
        protocol_errors=protocol_errors(events),
    )
    if row["protocol_errors"]:
        row.update(status="failed", task_success=False, failure_reason="tool_protocol_invalid")
    return row


def execute_case(job, client, ledger, phase, manifest):
    try:
        return _execute_case(job, client, ledger, phase, manifest)
    except Exception as exc:
        index, case, label, repetition = job
        return {
            "case_id": case["case_id"],
            "category": case["category"],
            "experiment_variant": label,
            "repeat": repetition,
            "schedule_index": index,
            "status": "error",
            "task_success": False,
            "failure_reason": type(exc).__name__,
            "failure_category": "measurement_or_setup_error",
            "phase": phase,
            "protocol_hash": manifest["protocol_hash"],
            "usage_source": "provider",
        }


def schedule(cases, repeat):
    # Paired variants rotate per task/repeat, preventing a fixed variant from always running first.
    jobs = []
    variants = CONFIG["variants"]
    for repetition in range(1, repeat + 1):
        for index, case in enumerate(cases):
            shift = (index + repetition - 1) % len(variants)
            for label in variants[shift:] + variants[:shift]:
                jobs.append((len(jobs), case, label, repetition))
    return jobs


def pilot_valid(rows: list[dict]) -> bool:
    return (
        len(rows) == CONFIG["pilot_cases"] * len(CONFIG["variants"])
        and all(
            r.get("task_success") is True
            and r.get("model_response_count", 0) == r.get("model_calls", 0) > 0
            and r.get("input_tokens") is not None
            and r.get("output_tokens") is not None
            and not r.get("protocol_errors")
            and r.get("unsafe_side_effects") == 0
            and r.get("resume_success") is not False
            and (
                not r.get("expected", {}).get("requires_approval")
                or (
                    r.get("resume_success") is True
                    and (r.get("resume_attempts") or 0) > 0
                    and (r.get("service_restart_count") or 0) > 0
                )
            )
            for r in rows
        )
        and all(
            any(r.get("task_success") and r["experiment_variant"] == label for r in rows)
            for label in CONFIG["variants"]
        )
    )


def wilson(passed, n):
    if not n:
        return None
    z = 1.96
    p = passed / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [center - half, center + half]


def aggregate(rows):
    output = {}
    for label in CONFIG["variants"]:
        group = [r for r in rows if r["experiment_variant"] == label]
        if not group:
            continue
        passed = sum(bool(r.get("task_success")) for r in group)
        measured = [r for r in group if r.get("status") not in {"not_run", "incomplete"}]
        stats = {
            "planned_tasks": len(group),
            "executed_tasks": len(measured),
            "passed": passed,
            "task_success_rate": passed / len(group),
            "success_wilson95": wilson(passed, len(group)),
            "failures": [
                {
                    "case_id": r["case_id"],
                    "repeat": r["repeat"],
                    "status": r["status"],
                    "reason": r.get("failure_reason"),
                    "category": r.get("failure_category"),
                }
                for r in group
                if not r.get("task_success")
            ],
        }
        for key in ("sop_compliance", "memory_retention", "resume_success"):
            applicable = [r[key] for r in measured if r.get(key) is not None]
            stats[key] = {
                "passed": sum(bool(x) for x in applicable),
                "applicable": len(applicable),
                "rate": sum(bool(x) for x in applicable) / len(applicable) if applicable else None,
            }
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "tool_proposed_count",
            "tool_executed_count",
            "skill_routing_count",
            "skill_switch_count",
            "total_tool_executed_count",
            "unsafe_side_effects",
        ):
            values = [r[key] for r in measured if r.get(key) is not None]
            stats[key] = sum(values) if len(values) == len(measured) and measured else None
            stats[f"known_{key}"] = sum(values)
            stats[f"mean_{key}"] = stats[key] / len(measured) if stats[key] is not None else None
            successful = [r.get(key) for r in measured if r.get("task_success")]
            stats[f"successful_mean_{key}"] = (
                mean(successful) if successful and all(v is not None for v in successful) else None
            )
        latencies = sorted(r["latency_ms"] for r in measured if r.get("latency_ms") is not None)
        stats["latency_ms"] = (
            {
                "mean": mean(latencies),
                "median": median(latencies),
                "p95": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)],
            }
            if latencies
            else None
        )
        output[label] = stats
    return output


def publish_metrics(rows, budget, manifest, *, root=ROOT):
    fields = [
        "case_id",
        "category",
        "phase",
        "experiment_variant",
        "repeat",
        "status",
        "task_success",
        "failure_reason",
        "failure_category",
        "sop_compliance",
        "unsafe_side_effects",
        "memory_retention",
        "resume_success",
        "memory_retention_applicable",
        "resume_success_applicable",
        "model_calls",
        "tool_proposed_count",
        "tool_executed_count",
        "skill_routing_count",
        "skill_switch_count",
        "skill_model_calls",
        "skill_input_tokens",
        "skill_output_tokens",
        "skill_total_tokens",
        "total_tool_executed_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "usage_source",
        "git_sha",
        "source_hash",
        "dataset_hash",
        "model_config_hash",
        "protocol_hash",
        "raw_trace_sha256",
    ]
    artifact = root / "artifacts"
    artifact.mkdir(exist_ok=True)
    public_rows = [{key: row.get(key) for key in fields} for row in rows]
    with (artifact / "results.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(public_rows)
    final = [r for r in rows if r["phase"] == "final"]
    summary = {
        "protocol_hash": manifest["protocol_hash"],
        "config": CONFIG,
        "budget": {k: budget[k] for k in ("limits", "http_attempts", "cost_upper_usd", "actual_billed_usd", "stopped")},
        "pilot": aggregate([r for r in rows if r["phase"] == "pilot"]),
        "final": aggregate(final),
        "rows": public_rows,
        "notes": [
            "Provider usage only; actual billing unavailable.",
            "Wilson intervals treat runs as samples; repeated cases are correlated, not independent population trials.",
            "Task success includes all planned rows; not_run rows remain in the denominator.",
        ],
    }
    (artifact / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--execute", choices=["pilot", "final"])
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    if sum(bool(x) for x in (args.freeze, args.execute, args.export)) != 1:
        raise SystemExit("Choose exactly one of --freeze, --execute PHASE, --export")
    os.chdir(ROOT)
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.freeze:
        print(json.dumps({"protocol_hash": freeze()["protocol_hash"], "api_calls": 0}))
        return
    manifest = verify_manifest()
    if args.export and not (DIRECTORY / "budget.json").exists():
        raise SystemExit("no_existing_experiment_to_export")
    ledger = BudgetLedger(DIRECTORY / "budget.json", limits=CONFIG["limits"])
    if args.export:
        rows = [
            r
            for phase in ("pilot", "final")
            if (DIRECTORY / f"{phase}_results.json").exists()
            for r in json.loads((DIRECTORY / f"{phase}_results.json").read_text())
        ]
        publish_metrics(rows, ledger.summary(), manifest)
        print("Exported public metrics; API calls=0")
        return
    if datetime.now(UTC).date().isoformat() != CONFIG["limits"]["pricing_verified_on"]:
        raise SystemExit("pricing_review_required_for_new_utc_date")
    # Refuse dirty code; experiment manifests and results must point to a reviewable commit.
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise SystemExit("commit_frozen_experiment_before_paid_execution")
    os.environ.update(
        SUPPORT_MODEL_PROVIDER="deepseek",
        DEEPSEEK_MODEL="deepseek-flash",
        DEEPSEEK_BASE_URL="https://api.deepseek.com",
        DEEPSEEK_MAX_TOKENS="1024",
        DEEPSEEK_TIMEOUT="60",
        DEEPSEEK_MAX_RETRIES="0",
        DEEPSEEK_TEMPERATURE="0",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        LANGCHAIN_TRACING="false",
    )
    load_key(args.env_file)
    claim_phase(ledger, args.execute, manifest["protocol_hash"])
    cases = json.loads((ROOT / f"data/benchmark_{'dev' if args.execute == 'pilot' else 'final'}.json").read_text())
    cases = cases[: CONFIG["pilot_cases"]] if args.execute == "pilot" else cases
    jobs = schedule(cases, 1 if args.execute == "pilot" else CONFIG["repeat"])
    results = [
        {
            "case_id": case["case_id"],
            "category": case["category"],
            "experiment_variant": label,
            "repeat": repetition,
            "schedule_index": index,
            "status": "incomplete",
            "task_success": False,
            "failure_reason": "no_saved_result",
            "phase": args.execute,
            "protocol_hash": manifest["protocol_hash"],
            "usage_source": "provider",
        }
        for index, case, label, repetition in jobs
    ]
    result_path = DIRECTORY / f"{args.execute}_results.json"
    result_path.write_text(json.dumps(results, indent=2) + "\n")
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    transport = DatedTransport(ledger, httpx.HTTPTransport(retries=0, proxy=proxy))
    with (
        httpx.Client(transport=transport, follow_redirects=False, timeout=60) as client,
        ThreadPoolExecutor(max_workers=CONFIG["parallel_cases"]) as pool,
    ):
        futures = {pool.submit(execute_case, job, client, ledger, args.execute, manifest): job[0] for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            row = future.result()
            results[futures[future]] = row
            temp = result_path.with_suffix(".tmp")
            temp.write_text(json.dumps(results, indent=2) + "\n")
            os.replace(temp, result_path)
            print(
                json.dumps(
                    {
                        "phase": args.execute,
                        "done": done,
                        "planned": len(jobs),
                        "case_id": row["case_id"],
                        "variant": row["experiment_variant"],
                        "status": row["status"],
                    }
                ),
                flush=True,
            )
    validated = (
        pilot_valid(results)
        if args.execute == "pilot"
        else all(
            r.get("status") not in {"not_run", "incomplete", "error"}
            and r.get("unsafe_side_effects") == 0
            and r.get("model_response_count", 0) == r.get("model_calls", 0) > 0
            and r.get("total_tokens") is not None
            and not r.get("protocol_errors")
            for r in results
        )
    )
    with ledger.edit() as state:
        state["phases"][args.execute].update(completed=True, validated=validated, rows=len(results))
    print(
        json.dumps(
            {
                "phase": args.execute,
                "validated": validated,
                "http_attempts": ledger.summary()["http_attempts"],
                "cost_upper_usd": ledger.summary()["cost_upper_usd"],
            }
        ),
        flush=True,
    )
    if not validated:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
