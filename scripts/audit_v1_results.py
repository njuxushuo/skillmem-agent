"""Read-only V1 rejudging with Oracle V2. No model, network, database or business execution.

Full traces remain private; output contains deterministic score/evidence indices only.
Missing/corrupt evidence produces an explicit uncovered row, never an invented score.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_evidence(row: dict, *, trace_root: Path | None = None) -> tuple[dict, list[dict]]:
    path = Path(row["raw_trace_path"])
    if trace_root is not None:
        path = trace_root / path.parent.name / path.name
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["raw_trace_sha256"]:
        raise ValueError("trace_hash_mismatch")
    events = [json.loads(line) for line in raw.splitlines()]
    if any(e.get("thread_id") != row["thread_id"] for e in events):
        raise ValueError("trace_thread_mismatch")
    start = next(e for e in events if e["event"] == "task.start")
    state = next(e for e in reversed(events) if e["event"] == "task.state")
    reply = next(e["result"]["reply"] for e in reversed(events) if e["event"] == "action.result")
    case = start["case"]
    label = row["experiment_variant"]
    if label not in {"B1", "M", "MS", "B1_SOP"}:
        raise ValueError("unknown_experiment_variant")
    if start.get("variant") != ("B1" if label == "B1_SOP" else label) or bool(start.get("full_sop")) != (
        label == "B1_SOP"
    ):
        raise ValueError("trace_variant_mismatch")
    for field in ("git_sha", "source_hash", "dataset_hash", "fixture_hash", "policy_hash", "skill_sop_hash"):
        if row.get(field) != start.get("provenance", {}).get(field):
            raise ValueError("trace_provenance_mismatch")
    if (
        row.get("model_config_hash")
        != hashlib.sha256(json.dumps(start["model_config"], sort_keys=True, default=str).encode()).hexdigest()
    ):
        raise ValueError("trace_model_config_mismatch")
    if case["case_id"] != row["case_id"]:
        raise ValueError("trace_case_mismatch")
    approvals, pending = [], None
    for event in events:
        if event["event"] == "graph.interrupt":
            pending = event["details"]
        elif event["event"] == "graph.resume":
            if pending is None:
                raise ValueError("resume_without_interrupt")
            approvals.append({**event["decision"], "order_id": pending["order_id"], "amount": pending["amount"]})
    completed = (
        row.get("turns_completed") == len(case["turns"])
        and len(approvals) == len(case.get("approval_script", []))
        and not any(e["event"] in {"graph.error", "execution.stopped"} for e in events)
    )
    return {
        "case": case,
        "before": state["business_before"],
        "after": state["business_after"],
        "reply": reply,
        "completed": completed,
        "approvals": approvals,
    }, events


def audit(input_path: Path, *, trace_root: Path | None = None) -> dict:
    from support_agent.evaluation import EvalCase, judge
    from support_agent.oracle_v2 import judge_v2, oracle_metadata

    input_hash = file_hash(input_path)
    original = json.loads(input_path.read_text())
    identities = [(r["case_id"], r["experiment_variant"], r["repeat"]) for r in original]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate_run_identity")
    annotations_path = ROOT / "artifacts/oracle_audit.json"
    annotations = json.loads(annotations_path.read_text())["annotations"] if annotations_path.exists() else []
    labels = {(r["case_id"], r["variant"], r["repeat"]): r["audit_label"] for r in annotations}
    output = []
    for row, identity in zip(original, identities, strict=True):
        result = {
            "case_id": identity[0],
            "variant": identity[1],
            "repeat": identity[2],
            "category": row.get("category"),
            "v1_frozen_score": row["task_success"],
            "v1_failure_reason": row.get("failure_reason"),
            "historical_audit_label": labels.get(identity),
            "historical_dispute_retained": labels.get(identity)
            in {"correct_order_use_missing_explicit_final_reference", "safe_refusal_missing_required_business_reason"},
            "source_trace_sha256": row.get("raw_trace_sha256"),
            "source_git_sha": row.get("git_sha"),
            "source_protocol_hash": row.get("protocol_hash"),
            "oracle_v2_diagnostic_score": None,
            "evidence_status": "unavailable",
        }
        try:
            evidence, events = read_evidence(row, trace_root=trace_root)
            evidence["case"] = EvalCase.model_validate(evidence["case"])
            old = judge(**evidence, events=events)
            if old["task_success"] != row["task_success"] or old["failure_reason"] != row.get("failure_reason"):
                raise ValueError("v1_oracle_reproduction_mismatch")
            verdict = judge_v2(**evidence, events=events)
            result.update(
                evidence_status="verified",
                oracle_v2=verdict,
                oracle_v2_diagnostic_score=verdict["full_task_success"],
                trace_run_id=events[0]["run_id"],
                state_step=next(e["step"] for e in reversed(events) if e["event"] == "task.state"),
                evidence_index=[
                    {k: e.get(k) for k in ("event", "step", "tool_call_id", "tool_name")}
                    for e in events
                    if e["event"] in {"tool.execution_end", "graph.interrupt", "graph.resume", "execution.stopped"}
                ],
            )
        except (OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
            result["evidence_error"] = type(exc).__name__
            known_codes = {
                "trace_hash_mismatch",
                "trace_thread_mismatch",
                "trace_case_mismatch",
                "resume_without_interrupt",
                "unknown_experiment_variant",
                "trace_variant_mismatch",
                "trace_provenance_mismatch",
                "trace_model_config_mismatch",
                "v1_oracle_reproduction_mismatch",
            }
            result["evidence_error_code"] = (
                str(exc) if type(exc) is ValueError and str(exc) in known_codes else "missing_or_invalid_evidence"
            )
        output.append(result)
    if file_hash(input_path) != input_hash:
        raise RuntimeError("input_changed_during_readonly_audit")
    summary = {}
    for variant in sorted({r["variant"] for r in output}):
        group = [r for r in output if r["variant"] == variant]
        covered = [r for r in group if r["evidence_status"] == "verified"]
        summary[variant] = {
            "runs": len(group),
            "distinct_cases": len({r["case_id"] for r in group}),
            "covered_runs": len(covered),
            "v1_passed": sum(r["v1_frozen_score"] for r in group),
            "oracle_v2_diagnostic_passed": sum(r["oracle_v2_diagnostic_score"] is True for r in covered),
            "changed": sum(r["v1_frozen_score"] != r["oracle_v2_diagnostic_score"] for r in covered),
            "layers": {
                key: dict(Counter(str(r["oracle_v2"].get(key)) for r in covered))
                for key in (
                    "business_success",
                    "hard_safety_pass",
                    "soft_sop_pass",
                    "safety_sop_pass",
                    "response_facts_pass",
                    "full_task_success",
                    "needs_review",
                )
            },
        }
    return {
        "audit_version": "v1_1_readonly_rejudge_1",
        "interpretation": "Post-hoc diagnostic; not independent new model performance.",
        "input_sha256": input_hash,
        "historical_annotations_sha256": file_hash(annotations_path) if annotations_path.exists() else None,
        "oracle": oracle_metadata(),
        "api_calls": 0,
        "input_rows": len(original),
        "covered_rows": sum(r["evidence_status"] == "verified" for r in output),
        "variants": summary,
        "rows": output,
    }


def write_outputs(report: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "audit_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    fields = [
        "case_id",
        "variant",
        "repeat",
        "category",
        "evidence_status",
        "v1_frozen_score",
        "oracle_v2_diagnostic_score",
        "business_success",
        "hard_safety_pass",
        "soft_sop_pass",
        "safety_sop_pass",
        "response_facts_pass",
        "needs_review",
        "v1_failure_reason",
        "historical_audit_label",
        "source_trace_sha256",
        "trace_run_id",
        "state_step",
    ]
    with (output / "audit_results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in report["rows"]:
            combined = {**row, **row.get("oracle_v2", {})}
            writer.writerow({k: combined.get(k) for k in fields})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / ".local/stage5/final_results.json")
    parser.add_argument("--trace-root", type=Path, help="Relocate traces: ROOT/run_id/1-0.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/v1_1")
    args = parser.parse_args()
    from run_offline import configure_offline

    configure_offline()
    if args.output.resolve() == args.input.resolve().parent or args.output.resolve() == ROOT / "artifacts":
        raise SystemExit("Use a distinct versioned output directory; V1 evidence is read-only")
    report = audit(args.input, trace_root=args.trace_root)
    write_outputs(report, args.output)
    print(json.dumps({k: report[k] for k in ("input_rows", "covered_rows", "api_calls", "variants")}))
    if report["covered_rows"] != report["input_rows"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
