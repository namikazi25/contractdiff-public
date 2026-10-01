#!/usr/bin/env python3
"""Evaluate frozen AI, baseline, and union outputs per document."""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "evaluation"
BROWSER_OUTPUT = ROOT / "prototype" / "data" / "evaluation.js"

SAMPLE_WARNING = (
    "This is an 11-document synthetic smoke test. It is used to test the "
    "harness and expose failure modes. It is not a legal benchmark."
)
OPERATIONAL_ISSUES = {"prompt_injection_detected"}

COLUMNS = (
    "document_id",
    "expected_contract_type",
    "actual_contract_type",
    "expected_findings",
    "ai_hits",
    "ai_misses",
    "ai_false_alarms",
    "baseline_hits",
    "baseline_misses",
    "baseline_false_alarms",
    "union_hits",
    "union_misses",
    "union_false_alarms",
    "expected_route",
    "actual_route",
    "route_correct",
    "high_risk_miss",
    "notes",
    "unsupported_routed",
    "duplicate_detected",
    "validation_failure",
    "ai_prompt_tokens",
    "ai_completion_tokens",
    "ai_total_tokens",
    "ai_wall_time_ms",
    "ai_cost_usd",
    "baseline_wall_time_ms",
)

JSON_COLUMNS = {
    "expected_findings",
    "ai_hits",
    "ai_misses",
    "ai_false_alarms",
    "baseline_hits",
    "baseline_misses",
    "baseline_false_alarms",
    "union_hits",
    "union_misses",
    "union_false_alarms",
}
BOOLEAN_COLUMNS = {
    "route_correct",
    "high_risk_miss",
    "unsupported_routed",
    "duplicate_detected",
    "validation_failure",
}
INTEGER_COLUMNS = {
    "ai_prompt_tokens",
    "ai_completion_tokens",
    "ai_total_tokens",
    "ai_wall_time_ms",
    "baseline_wall_time_ms",
}
FLOAT_COLUMNS = {"ai_cost_usd"}


class EvaluationError(ValueError):
    """Raised when frozen evaluation inputs are incomplete or inconsistent."""


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise EvaluationError(f"required evaluation input is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(
            f"invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}"
        ) from exc


def index_by_document(records: Iterable[dict[str, Any]], source: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        document_id = record.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise EvaluationError(f"{source} contains a record without document_id")
        if document_id in indexed:
            raise EvaluationError(f"{source} contains duplicate document_id {document_id}")
        indexed[document_id] = record
    return indexed


def finding_issues(output: dict[str, Any]) -> set[str]:
    return {
        finding["candidate_issue"]
        for finding in output.get("findings", [])
        if finding.get("candidate_issue") not in OPERATIONAL_ISSUES
    }


def union_issues(routing: dict[str, Any]) -> set[str]:
    return {
        finding["candidate_issue"]
        for finding in routing.get("union_findings", [])
        if finding.get("candidate_issue") not in OPERATIONAL_ISSUES
        and finding.get("severity") is not None
    }


def compare_issues(expected: set[str], actual: set[str]) -> tuple[list[str], list[str], list[str]]:
    return (
        sorted(expected & actual),
        sorted(expected - actual),
        sorted(actual - expected),
    )


def audit_metrics(output: dict[str, Any]) -> dict[str, int | float | None]:
    audit = output.get("audit") or {}
    usage = audit.get("usage") or {}
    return {
        "ai_prompt_tokens": usage.get("prompt_tokens"),
        "ai_completion_tokens": usage.get("completion_tokens"),
        "ai_total_tokens": usage.get("total_tokens"),
        "ai_wall_time_ms": audit.get("wall_time_ms"),
        "ai_cost_usd": audit.get("cost_usd"),
    }


def failure_sentences(
    *,
    document_id: str,
    comparisons: dict[str, tuple[list[str], list[str], list[str]]],
    routing: dict[str, Any],
) -> list[str]:
    labels = {"ai": "AI", "baseline": "Baseline", "union": "Union"}
    sentences = []
    for path in ("ai", "baseline", "union"):
        _, misses, false_alarms = comparisons[path]
        for issue in misses:
            sentences.append(f"{labels[path]} missed expected issue {issue}.")
        for issue in false_alarms:
            sentences.append(
                f"{labels[path]} raised {issue}, but it is absent from the expected labels."
            )

    has_injection_signal = any(
        item.get("candidate_issue") == "prompt_injection_detected"
        for item in routing.get("union_findings", [])
    )
    if document_id == "NDA-006" and has_injection_signal:
        sentences.append(
            "The embedded instruction was surfaced as an operational safety signal, "
            "not scored as a legal finding."
        )
    if document_id == "NDA-008" and "unlimited_liability" in comparisons["baseline"][1]:
        sentences.append(
            "The AI caught reworded unlimited liability that the deterministic baseline "
            "was designed to miss."
        )
    return sentences


def evaluate_records(
    expected_labels: list[dict[str, Any]],
    model_outputs: list[dict[str, Any]],
    baseline_outputs: list[dict[str, Any]],
    routing_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_by_id = index_by_document(expected_labels, "expected_labels.json")
    ai_by_id = index_by_document(model_outputs, "model_outputs.json")
    baseline_by_id = index_by_document(baseline_outputs, "baseline_outputs.json")
    routing_by_id = index_by_document(routing_records, "routing.json")

    expected_ids = set(expected_by_id)
    for source, indexed in (
        ("model_outputs.json", ai_by_id),
        ("baseline_outputs.json", baseline_by_id),
        ("routing.json", routing_by_id),
    ):
        if set(indexed) != expected_ids:
            missing = sorted(expected_ids - set(indexed))
            extra = sorted(set(indexed) - expected_ids)
            raise EvaluationError(
                f"{source} document IDs do not match expected labels; "
                f"missing={missing}, extra={extra}"
            )

    records = []
    for expected in expected_labels:
        document_id = expected["document_id"]
        ai = ai_by_id[document_id]
        baseline = baseline_by_id[document_id]
        routing = routing_by_id[document_id]
        expected_findings = sorted(
            expected.get("expected_findings", []),
            key=lambda item: item["candidate_issue"],
        )
        expected_issues = {
            finding["candidate_issue"] for finding in expected_findings
        }
        expected_severity = {
            finding["candidate_issue"]: finding["severity"]
            for finding in expected_findings
        }
        comparisons = {
            "ai": compare_issues(expected_issues, finding_issues(ai)),
            "baseline": compare_issues(expected_issues, finding_issues(baseline)),
            "union": compare_issues(expected_issues, union_issues(routing)),
        }
        ai_hits, ai_misses, ai_false_alarms = comparisons["ai"]
        baseline_hits, baseline_misses, baseline_false_alarms = comparisons["baseline"]
        union_hits, union_misses, union_false_alarms = comparisons["union"]
        actual_route = routing["final_route"]
        ai_audit = audit_metrics(ai)
        baseline_audit = baseline.get("audit") or {}
        notes = failure_sentences(
            document_id=document_id,
            comparisons=comparisons,
            routing=routing,
        )

        record = {
            "document_id": document_id,
            "expected_contract_type": expected["expected_contract_type"],
            "actual_contract_type": ai["contract_type"],
            "expected_findings": expected_findings,
            "ai_hits": ai_hits,
            "ai_misses": ai_misses,
            "ai_false_alarms": ai_false_alarms,
            "baseline_hits": baseline_hits,
            "baseline_misses": baseline_misses,
            "baseline_false_alarms": baseline_false_alarms,
            "union_hits": union_hits,
            "union_misses": union_misses,
            "union_false_alarms": union_false_alarms,
            "expected_route": expected["expected_route"],
            "actual_route": actual_route,
            "route_correct": actual_route == expected["expected_route"],
            "high_risk_miss": any(
                expected_severity[issue] == "high" for issue in union_misses
            ),
            "notes": " ".join(notes),
            "unsupported_routed": (
                expected["expected_contract_type"] == "unknown"
                and actual_route == "manual_review"
            ),
            "duplicate_detected": (
                bool(expected.get("is_duplicate")) and actual_route == "duplicate"
            ),
            "validation_failure": (
                ai.get("status") == "analysis_failed"
                or baseline.get("status") == "analysis_failed"
            ),
            **ai_audit,
            "baseline_wall_time_ms": baseline_audit.get("wall_time_ms"),
        }
        if tuple(record) != COLUMNS:
            raise EvaluationError("internal evaluation column order is inconsistent")
        records.append(record)
    return records


def evaluate_from_data(data_dir: Path = DATA_DIR) -> list[dict[str, Any]]:
    return evaluate_records(
        load_json(data_dir / "expected_labels.json"),
        load_json(data_dir / "model_outputs.json"),
        load_json(data_dir / "baseline_outputs.json"),
        load_json(data_dir / "routing.json"),
    )


def _csv_value(column: str, value: Any) -> Any:
    if column in JSON_COLUMNS:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if column in BOOLEAN_COLUMNS:
        return "true" if value else "false"
    if value is None:
        return ""
    return value


def render_csv(records: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow(
            {column: _csv_value(column, record[column]) for column in COLUMNS}
        )
    return stream.getvalue()


def render_json(records: list[dict[str, Any]]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def write_results(
    records: list[dict[str, Any]],
    output_dir: Path = OUTPUT_DIR,
) -> tuple[Path, Path]:
    csv_path = output_dir / "results.csv"
    json_path = output_dir / "results.json"
    _atomic_write(csv_path, render_csv(records))
    _atomic_write(json_path, render_json(records))
    return csv_path, json_path


def read_csv_results(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    records = []
    for row in rows:
        record: dict[str, Any] = {}
        for column in COLUMNS:
            value = row[column]
            if column in JSON_COLUMNS:
                record[column] = json.loads(value)
            elif column in BOOLEAN_COLUMNS:
                record[column] = value == "true"
            elif column in INTEGER_COLUMNS:
                record[column] = int(value) if value else None
            elif column in FLOAT_COLUMNS:
                record[column] = float(value) if value else None
            else:
                record[column] = value
        records.append(record)
    return records


def export_browser_data(results_path: Path, browser_output: Path) -> Path:
    sys.path.insert(0, str(ROOT / "pipeline"))
    import export_browser_data as browser_exporter

    return browser_exporter.export_one(
        results_path,
        browser_output,
        data_key="evaluation",
        source_label="evaluation/results.json",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--browser-output", type=Path, default=BROWSER_OUTPUT)
    parser.add_argument(
        "--no-browser-export",
        action="store_true",
        help="write CSV/JSON without refreshing prototype/data/evaluation.js",
    )
    args = parser.parse_args(argv)
    try:
        records = evaluate_from_data(args.data_dir)
        csv_path, json_path = write_results(records, args.output_dir)
        browser_path = None
        if not args.no_browser_export:
            browser_path = export_browser_data(json_path, args.browser_output)
    except (OSError, EvaluationError) as exc:
        logger.error("Evaluation failed: %s", exc)
        return 1

    logger.info("Evaluated %d documents", len(records))
    logger.info("Wrote %s", csv_path)
    logger.info("Wrote %s", json_path)
    if browser_path:
        logger.info("Refreshed %s", browser_path)
    logger.info(SAMPLE_WARNING)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
