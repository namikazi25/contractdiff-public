#!/usr/bin/env python3
"""Apply playbooks and deterministically route both analysis paths."""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from apply_playbook import apply_playbook, select_playbook, union_findings

logger = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_PATH = DATA_DIR / "routing.json"
LOW_CONFIDENCE_THRESHOLD = 0.8

ROUTE_PRIORITY = {
    "duplicate": 0,
    "quick_check": 1,
    "legal_review": 2,
    "senior_legal_review": 3,
    "manual_review": 4,
}


class RoutingError(ValueError):
    """Raised when routing inputs are incomplete or internally inconsistent."""


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def detect_duplicates(contracts: list[dict[str, Any]]) -> dict[str, str]:
    """Map later documents to the earliest document with the same SHA-256."""

    ordered = sorted(
        enumerate(contracts),
        key=lambda pair: (pair[1]["received_at"], pair[0]),
    )
    first_by_hash = {}
    duplicates = {}
    for _, contract in ordered:
        file_hash = contract.get("file_hash")
        if not file_hash:
            raise RoutingError(
                f"{contract.get('document_id')}: missing file_hash for duplicate check"
            )
        original_id = first_by_hash.setdefault(file_hash, contract["document_id"])
        if original_id != contract["document_id"]:
            duplicates[contract["document_id"]] = original_id
    return duplicates


def route_path(
    output: dict[str, Any],
    enriched_findings: list[dict[str, Any]],
    *,
    contract: dict[str, Any],
    duplicate_of: str | None,
    confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
) -> str:
    """Apply the routing table in precedence order."""

    if duplicate_of is not None:
        return "duplicate"
    if contract["contract_type"] == "unknown":
        return "manual_review"
    status = output.get("status")
    if status == "analysis_failed":
        return "manual_review"
    if status != "completed":
        raise RoutingError(
            f"{contract['document_id']}: unknown analysis status {status!r}"
        )

    severities = {
        finding["severity"]
        for finding in enriched_findings
        if finding["severity"] is not None
    }
    if "high" in severities:
        return "senior_legal_review"
    if severities & {"medium", "low"}:
        return "legal_review"

    confidence = output.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise RoutingError(
            f"{contract['document_id']}: confidence must be numeric"
        )
    if not 0 <= confidence <= 1:
        raise RoutingError(
            f"{contract['document_id']}: confidence must be between 0 and 1"
        )
    return "legal_review" if confidence < confidence_threshold else "quick_check"


def safer_route(*routes: str) -> str:
    """Choose the route requiring the greatest level of human review."""

    try:
        return max(routes, key=ROUTE_PRIORITY.__getitem__)
    except KeyError as exc:
        raise RoutingError(f"unknown route {exc.args[0]!r}") from exc


def _outputs_by_document(
    outputs: list[dict[str, Any]],
    *,
    analysis_path: str,
    expected_ids: set[str],
) -> dict[str, dict[str, Any]]:
    mapped = {}
    for output in outputs:
        document_id = output.get("document_id")
        if document_id in mapped:
            raise RoutingError(
                f"{analysis_path}: duplicate output for {document_id!r}"
            )
        if output.get("analysis_path") != analysis_path:
            raise RoutingError(
                f"{document_id}: expected analysis_path {analysis_path!r}"
            )
        mapped[document_id] = output
    if set(mapped) != expected_ids:
        missing = sorted(expected_ids - set(mapped))
        extra = sorted(set(mapped) - expected_ids)
        raise RoutingError(
            f"{analysis_path}: output IDs do not match contracts; "
            f"missing={missing}, extra={extra}"
        )
    return mapped


def _validate_output_context(
    output: dict[str, Any],
    *,
    contract: dict[str, Any],
    playbook: dict[str, Any] | None,
) -> None:
    expected_playbook_id = None if playbook is None else playbook["playbook_id"]
    if output.get("playbook_id") != expected_playbook_id:
        raise RoutingError(
            f"{contract['document_id']}: candidate output used the wrong playbook"
        )
    if output.get("document_id") != contract["document_id"]:
        raise RoutingError(
            f"{contract['document_id']}: candidate output belongs to another document"
        )
    if output.get("status") == "analysis_failed":
        if output.get("findings") != [] or output.get("confidence") != 0:
            raise RoutingError(
                f"{contract['document_id']}: malformed analysis_failed marker"
            )


def build_routing(
    *,
    contracts: list[dict[str, Any]],
    playbooks: list[dict[str, Any]],
    model_outputs: list[dict[str, Any]],
    baseline_outputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the complete routing artifact in canonical contract order."""

    contract_ids = {contract["document_id"] for contract in contracts}
    if len(contract_ids) != len(contracts):
        raise RoutingError("contract document IDs must be unique")
    ai_by_id = _outputs_by_document(
        model_outputs,
        analysis_path="ai",
        expected_ids=contract_ids,
    )
    baseline_by_id = _outputs_by_document(
        baseline_outputs,
        analysis_path="baseline",
        expected_ids=contract_ids,
    )
    duplicates = detect_duplicates(contracts)
    routing = []

    for contract in contracts:
        document_id = contract["document_id"]
        playbook = select_playbook(contract, playbooks)
        ai_output = ai_by_id[document_id]
        baseline_output = baseline_by_id[document_id]
        _validate_output_context(ai_output, contract=contract, playbook=playbook)
        _validate_output_context(
            baseline_output,
            contract=contract,
            playbook=playbook,
        )

        ai_findings = apply_playbook(
            ai_output.get("findings", []),
            contract=contract,
            playbook=playbook,
        )
        baseline_findings = apply_playbook(
            baseline_output.get("findings", []),
            contract=contract,
            playbook=playbook,
        )
        duplicate_of = duplicates.get(document_id)
        ai_route = route_path(
            ai_output,
            ai_findings,
            contract=contract,
            duplicate_of=duplicate_of,
        )
        baseline_route = route_path(
            baseline_output,
            baseline_findings,
            contract=contract,
            duplicate_of=duplicate_of,
        )
        routing.append(
            {
                "document_id": document_id,
                "duplicate_of": duplicate_of,
                "playbook_id": None if playbook is None else playbook["playbook_id"],
                "playbook_version": None if playbook is None else playbook["version"],
                "paths": {
                    "ai": {
                        "status": ai_output["status"],
                        "confidence": ai_output["confidence"],
                        "findings": ai_findings,
                        "route": ai_route,
                    },
                    "baseline": {
                        "status": baseline_output["status"],
                        "confidence": baseline_output["confidence"],
                        "findings": baseline_findings,
                        "route": baseline_route,
                    },
                },
                "union_findings": union_findings(ai_findings, baseline_findings),
                "final_route": safer_route(ai_route, baseline_route),
            }
        )
    return routing


def write_routing(path: Path, routing: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(routing, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    try:
        contracts = load_json(args.data_dir / "contracts.json")
        playbooks = load_json(args.data_dir / "playbook.json")
        model_outputs = load_json(args.data_dir / "model_outputs.json")
        baseline_outputs = load_json(args.data_dir / "baseline_outputs.json")
        routing = build_routing(
            contracts=contracts,
            playbooks=playbooks,
            model_outputs=model_outputs,
            baseline_outputs=baseline_outputs,
        )
        write_routing(args.output, routing)
    except (OSError, json.JSONDecodeError, RoutingError, ValueError) as exc:
        logger.error("Routing failed: %s", exc)
        return 1
    logger.info("Wrote %d routing records to %s", len(routing), args.output)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
