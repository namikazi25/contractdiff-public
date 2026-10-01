"""Routing-table, union, duplicate, and pinned-route tests."""
import json
import logging
from pathlib import Path

import pytest

import route_contract

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def load(name):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def contracts():
    return load("contracts.json")


@pytest.fixture(scope="module")
def playbooks():
    return load("playbook.json")


def bare_output(contract, *, path="ai", status="completed", confidence=0.9):
    playbook_id = {
        "mutual_nda": "PB-MNDA-UK-1.0",
        "order_form": "PB-ORDER-UK-1.0",
        "unknown": None,
    }[contract["contract_type"]]
    return {
        "document_id": contract["document_id"],
        "analysis_path": path,
        "status": status,
        "contract_type": contract["contract_type"],
        "playbook_id": playbook_id,
        "findings": [],
        "confidence": confidence,
    }


def enriched(severity):
    return [{"candidate_issue": "issue", "severity": severity}]


@pytest.mark.parametrize(
    ("duplicate_of", "contract_type", "status", "severity", "confidence", "expected"),
    [
        ("NDA-001", "mutual_nda", "analysis_failed", "high", 0.1, "duplicate"),
        (None, "unknown", "completed", None, 0.9, "manual_review"),
        (None, "mutual_nda", "analysis_failed", None, 0.9, "manual_review"),
        (None, "mutual_nda", "completed", "high", 0.9, "senior_legal_review"),
        (None, "mutual_nda", "completed", "medium", 0.9, "legal_review"),
        (None, "mutual_nda", "completed", "low", 0.9, "legal_review"),
        (None, "mutual_nda", "completed", None, 0.79, "legal_review"),
        (None, "mutual_nda", "completed", None, 0.8, "quick_check"),
    ],
)
def test_routing_table_precedence(
    duplicate_of,
    contract_type,
    status,
    severity,
    confidence,
    expected,
):
    contract = {"document_id": "TEST-001", "contract_type": contract_type}
    output = {"status": status, "confidence": confidence}
    findings = [] if severity is None else enriched(severity)
    assert route_contract.route_path(
        output,
        findings,
        contract=contract,
        duplicate_of=duplicate_of,
    ) == expected


def test_union_chooses_safer_route():
    assert route_contract.safer_route(
        "quick_check", "senior_legal_review"
    ) == "senior_legal_review"
    assert route_contract.safer_route(
        "senior_legal_review", "manual_review"
    ) == "manual_review"


def test_exact_duplicate_maps_to_earliest_document(contracts):
    duplicates = route_contract.detect_duplicates(contracts)
    assert duplicates == {"NDA-001-DUP": "NDA-001"}


def fixture_model_outputs(contracts):
    # The fixture intentionally proposes no issues. The committed baseline
    # supplies the obvious findings, exercising the safer union behaviour.
    return [bare_output(contract) for contract in contracts]


def test_all_pinned_document_routes(contracts, playbooks):
    routing = route_contract.build_routing(
        contracts=contracts,
        playbooks=playbooks,
        model_outputs=fixture_model_outputs(contracts),
        baseline_outputs=load("baseline_outputs.json"),
    )
    actual = {record["document_id"]: record["final_route"] for record in routing}
    expected = {
        label["document_id"]: label["expected_route"]
        for label in load("expected_labels.json")
    }
    # NDA-008 is the designed baseline miss. With an empty AI path
    # (this fixture) nothing catches its reworded clause, so it under-routes
    # to quick_check — exactly the gap the union exists to close. Every other
    # route is carried by the baseline alone.
    nda008_actual = actual.pop("NDA-008")
    nda008_expected = expected.pop("NDA-008")
    assert nda008_expected == "senior_legal_review"
    assert nda008_actual == "quick_check"
    assert actual == expected


def test_prompt_injection_routes_from_legal_issue_not_telemetry(contracts, playbooks):
    routing = route_contract.build_routing(
        contracts=contracts,
        playbooks=playbooks,
        model_outputs=fixture_model_outputs(contracts),
        baseline_outputs=load("baseline_outputs.json"),
    )
    record = next(item for item in routing if item["document_id"] == "NDA-006")
    union = {item["candidate_issue"]: item for item in record["union_findings"]}
    assert union["prompt_injection_detected"]["severity"] is None
    assert union["unlimited_liability"]["severity"] == "high"
    assert record["final_route"] == "senior_legal_review"


def test_analysis_failure_forces_union_to_manual_review(contracts, playbooks):
    model_outputs = fixture_model_outputs(contracts)
    failed = next(item for item in model_outputs if item["document_id"] == "NDA-001")
    failed["status"] = "analysis_failed"
    failed["confidence"] = 0
    routing = route_contract.build_routing(
        contracts=contracts,
        playbooks=playbooks,
        model_outputs=model_outputs,
        baseline_outputs=load("baseline_outputs.json"),
    )
    record = next(item for item in routing if item["document_id"] == "NDA-001")
    assert record["paths"]["ai"]["route"] == "manual_review"
    assert record["paths"]["baseline"]["route"] == "quick_check"
    assert record["final_route"] == "manual_review"


def test_batch_command_writes_routing_from_real_outputs(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    output_path = tmp_path / "routing.json"
    assert route_contract.main(
        [
            "--data-dir",
            str(DATA_DIR),
            "--output",
            str(output_path),
        ]
    ) == 0
    assert "Wrote 11 routing records" in caplog.text
    assert len(json.loads(output_path.read_text(encoding="utf-8"))) == 11


def test_real_routing_artifact_matches_fresh_build(contracts, playbooks):
    fresh = route_contract.build_routing(
        contracts=contracts,
        playbooks=playbooks,
        model_outputs=load("model_outputs.json"),
        baseline_outputs=load("baseline_outputs.json"),
    )
    assert load("routing.json") == fresh


def test_real_outputs_expose_only_the_nda_002_route_mismatch():
    expected = {
        label["document_id"]: label["expected_route"]
        for label in load("expected_labels.json")
    }
    mismatches = {
        record["document_id"]: {
            "expected": expected[record["document_id"]],
            "actual": record["final_route"],
        }
        for record in load("routing.json")
        if record["final_route"] != expected[record["document_id"]]
    }
    # NDA-002's AI false alarms (a phantom unlimited_liability among them)
    # escalate the clean reworded document to senior_legal_review — over-
    # escalation, the safe failure direction. An earlier temp-0 run failed
    # closed to manual_review instead; the failure modes move between runs
    assert mismatches == {
        "NDA-002": {
            "expected": "quick_check",
            "actual": "senior_legal_review",
        }
    }
