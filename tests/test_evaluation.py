"""Reproducibility, parity, and UI tests."""
import json
import logging
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from conftest import validate


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EVALUATION = ROOT / "evaluation"
PROTOTYPE = ROOT / "prototype"
sys.path.insert(0, str(EVALUATION))

import evaluate


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_evaluation_app(program):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    sources = ["global.window = {};"]
    for name in ["contracts", "templates", "playbook", "routing", "evaluation"]:
        sources.append(
            (PROTOTYPE / "data" / f"{name}.js").read_text(encoding="utf-8")
        )
    sources.extend(
        [
            (PROTOTYPE / "app.js").read_text(encoding="utf-8"),
            program,
        ]
    )
    result = subprocess.run(
        [node, "--input-type=commonjs"],
        input="\n".join(sources),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_frozen_results_are_exactly_reproducible():
    records = evaluate.evaluate_from_data(DATA)
    assert records == load_json(EVALUATION / "results.json")
    assert evaluate.render_json(records) == (
        EVALUATION / "results.json"
    ).read_text(encoding="utf-8")
    assert evaluate.render_csv(records) == (
        EVALUATION / "results.csv"
    ).read_text(encoding="utf-8")


def test_csv_and_json_carry_identical_typed_records():
    csv_records = evaluate.read_csv_results(EVALUATION / "results.csv")
    json_records = load_json(EVALUATION / "results.json")
    assert csv_records == json_records
    assert tuple(csv_records[0]) == evaluate.COLUMNS


def test_generated_records_pass_the_evaluation_schema(schemas, registry):
    for record in load_json(EVALUATION / "results.json"):
        validate(record, schemas["evaluation_result_v1.schema.json"], registry)


def test_observed_path_failures_remain_separate_and_honest():
    records = {
        item["document_id"]: item
        for item in load_json(EVALUATION / "results.json")
    }
    assert records["NDA-002"]["ai_false_alarms"] == [
        "governing_law_changed",
        "missing_termination_clause",
        "unlimited_liability",
    ]
    assert records["NDA-002"]["baseline_false_alarms"] == []
    assert records["NDA-005"]["ai_misses"] == [
        "missing_termination_clause"
    ]
    assert records["NDA-005"]["union_misses"] == []
    assert records["NDA-008"]["baseline_misses"] == [
        "unlimited_liability"
    ]
    assert records["NDA-008"]["ai_hits"] == ["unlimited_liability"]
    assert records["NDA-008"]["union_false_alarms"] == [
        "missing_termination_clause"
    ]


def test_operational_injection_signal_is_excluded_from_scoring():
    record = next(
        item
        for item in load_json(EVALUATION / "results.json")
        if item["document_id"] == "NDA-006"
    )
    scored_issues = [
        issue
        for key, value in record.items()
        if key.endswith(("_hits", "_misses", "_false_alarms"))
        for issue in value
    ]
    assert "prompt_injection_detected" not in scored_issues
    assert "operational safety signal" in record["notes"]


def test_every_miss_and_false_alarm_has_a_plain_failure_sentence():
    for record in load_json(EVALUATION / "results.json"):
        for path, label in (
            ("ai", "AI"),
            ("baseline", "Baseline"),
            ("union", "Union"),
        ):
            for issue in record[f"{path}_misses"]:
                assert f"{label} missed expected issue {issue}." in record["notes"]
            for issue in record[f"{path}_false_alarms"]:
                assert (
                    f"{label} raised {issue}, but it is absent from the "
                    "expected labels."
                ) in record["notes"]


def test_routes_come_from_routing_records_not_expected_labels():
    labels = load_json(DATA / "expected_labels.json")
    ai = load_json(DATA / "model_outputs.json")
    baseline = load_json(DATA / "baseline_outputs.json")
    routing = load_json(DATA / "routing.json")
    mutated = deepcopy(routing)
    record = next(item for item in mutated if item["document_id"] == "NDA-001")
    record["final_route"] = "legal_review"

    result = evaluate.evaluate_records(labels, ai, baseline, mutated)[0]
    assert result["expected_route"] == "quick_check"
    assert result["actual_route"] == "legal_review"
    assert result["route_correct"] is False


def test_summary_reports_control_counts_and_real_audit_metrics():
    output = run_evaluation_app(
        """
        console.log(JSON.stringify(
          window.QueueApp.buildEvaluationSummary(window.DATA.evaluation)
        ));
        """
    )
    summary = json.loads(output)
    assert summary["documents"] == 11
    assert summary["routeCorrect"] == 10
    assert summary["highRiskMisses"] == 0
    assert summary["paths"]["baseline"]["highRiskMisses"] == 1
    assert summary["unsupportedRouted"] == 1
    assert summary["duplicatesDetected"] == 1
    assert summary["validationFailures"] == 0
    assert summary["totalTokens"] == 56007
    assert summary["totalWallTimeMs"] == 46339
    assert summary["totalCostUsd"] == pytest.approx(0.0096036)
    assert summary["costCoverage"] == 11


def test_evaluation_screen_markup_loads_from_generated_browser_data():
    output = run_evaluation_app(
        """
        const html = window.QueueApp.evaluationMarkup(window.DATA.evaluation);
        console.log(JSON.stringify({
          length: html.length,
          documents: (html.match(/class="document-link"/g) || []).length,
          hasWarning: html.includes("This is an 11-document synthetic smoke test. It is used to test the harness and expose failure modes. It is not a legal benchmark."),
          hasProvenance: html.includes("Generated, not hard-coded"),
          hasHighRisk: html.includes("Union high-risk misses"),
          hasFailures: html.includes("What failed, in plain English"),
          hasUndefined: html.includes("undefined")
        }));
        """
    )
    rendered = json.loads(output)
    assert rendered["length"] > 10000
    assert rendered["documents"] == 11
    assert rendered["hasWarning"] is True
    assert rendered["hasProvenance"] is True
    assert rendered["hasHighRisk"] is True
    assert rendered["hasFailures"] is True
    assert rendered["hasUndefined"] is False

    html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
    app = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
    assert "data/evaluation.js" in html
    assert "data-evaluation-screen" in html
    assert 'parameters.get("view")' in app
    assert 'showEvaluation(data.evaluation)' in app
    smoke = (PROTOTYPE / "data-smoke-test.html").read_text(encoding="utf-8")
    assert 'src="data/evaluation.js"' in smoke
    assert '"evaluation"' in smoke


def test_cli_refreshes_all_outputs_from_a_temporary_directory(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    output_dir = tmp_path / "evaluation"
    browser_output = tmp_path / "prototype" / "data" / "evaluation.js"
    args = [
        "--data-dir",
        str(DATA),
        "--output-dir",
        str(output_dir),
        "--browser-output",
        str(browser_output),
    ]
    assert evaluate.main(args) == 0
    first = {
        path.name: path.read_bytes()
        for path in [
            output_dir / "results.csv",
            output_dir / "results.json",
            browser_output,
        ]
    }
    assert evaluate.main(args) == 0
    second = {
        path.name: path.read_bytes()
        for path in [
            output_dir / "results.csv",
            output_dir / "results.json",
            browser_output,
        ]
    }
    assert first == second
    assert evaluate.read_csv_results(output_dir / "results.csv") == load_json(
        output_dir / "results.json"
    )
    assert "Evaluated 11 documents" in caplog.text
    assert f"Refreshed {browser_output}" in caplog.text
    assert evaluate.SAMPLE_WARNING in caplog.text


def test_cli_logs_input_failure_without_a_traceback(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    assert evaluate.main(
        [
            "--data-dir",
            str(tmp_path / "missing-data"),
            "--output-dir",
            str(tmp_path / "evaluation"),
            "--no-browser-export",
        ]
    ) == 1
    assert "Evaluation failed: required evaluation input is missing" in caplog.text
    assert "Traceback" not in caplog.text
