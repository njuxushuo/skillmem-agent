"""Read-only historical audit must reject missing, crossed or changed evidence."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "audit_v1_results", Path(__file__).parents[1] / "scripts/audit_v1_results.py"
)
audit_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_script)


def evidence_fixture(tmp_path):
    from support_agent.evaluation import run_cases

    case = json.loads(Path("data/eval_tasks.json").read_text())[0]
    report = run_cases([case], tmp_path / "private", variant="B1")
    row = report["results"][0]
    path = Path(report["run_dir"]) / "1-0.jsonl"
    row.update(experiment_variant="B1", raw_trace_path=str(path), raw_trace_sha256=audit_script.file_hash(path))
    source = tmp_path / "original.json"
    source.write_text(json.dumps([row]))
    return source, path, row


def test_audit_deterministic_readonly_and_not_new_model_results(tmp_path):
    source, trace, _ = evidence_fixture(tmp_path)
    before = (source.read_bytes(), trace.read_bytes())
    first = audit_script.audit(source)
    second = audit_script.audit(source)
    assert first == second
    assert first["covered_rows"] == 1 and first["api_calls"] == 0
    assert first["rows"][0]["v1_frozen_score"] is True
    assert first["rows"][0]["oracle_v2"]["business_success"] is True
    assert (source.read_bytes(), trace.read_bytes()) == before
    audit_script.write_outputs(first, tmp_path / "new")
    public = (tmp_path / "new/audit_summary.json").read_text()
    assert str(trace) not in public
    assert "business_before" not in public


@pytest.mark.parametrize("defect", ["missing", "changed", "wrong_thread", "wrong_case"])
def test_missing_corrupt_or_crossed_trace_never_gets_score(tmp_path, defect):
    source, trace, row = evidence_fixture(tmp_path)
    if defect == "missing":
        trace.unlink()
    elif defect == "changed":
        trace.write_text(trace.read_text() + "\n")
    elif defect == "wrong_thread":
        row["thread_id"] = "wrong"
    else:
        row["case_id"] = "wrong"
    source.write_text(json.dumps([row]))
    result = audit_script.audit(source)
    assert result["covered_rows"] == 0
    assert result["rows"][0]["oracle_v2_diagnostic_score"] is None
    assert result["rows"][0]["evidence_status"] == "unavailable"


def test_duplicate_run_identity_rejected(tmp_path):
    source, _, row = evidence_fixture(tmp_path)
    source.write_text(json.dumps([row, row]))
    with pytest.raises(ValueError, match="duplicate_run_identity"):
        audit_script.audit(source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("experiment_variant", "MS"),
        ("experiment_variant", "B1_SOP"),
        ("git_sha", "wrong"),
        ("model_config_hash", "wrong"),
    ],
)
def test_variant_or_provenance_relabel_cannot_cover_trace(tmp_path, field, value):
    source, _, row = evidence_fixture(tmp_path)
    row[field] = value
    source.write_text(json.dumps([row]))
    report = audit_script.audit(source)
    assert report["covered_rows"] == 0
    assert report["rows"][0]["oracle_v2_diagnostic_score"] is None


def test_validation_error_cannot_publish_raw_input(tmp_path):
    source, trace, row = evidence_fixture(tmp_path)
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    start = next(e for e in events if e["event"] == "task.start")
    start["case"]["unexpected_private_field"] = "PII_CANARY_person_private@example.invalid"
    trace.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    row["raw_trace_sha256"] = audit_script.file_hash(trace)
    source.write_text(json.dumps([row]))
    report = audit_script.audit(source)
    assert report["covered_rows"] == 0
    assert "PII_CANARY" not in json.dumps(report)
    assert "example.invalid" not in json.dumps(report)
    assert report["rows"][0]["evidence_error_code"] == "missing_or_invalid_evidence"
