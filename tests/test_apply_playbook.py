"""Unit tests for deterministic playbook enforcement."""
import copy
import json
from pathlib import Path

import pytest

import apply_playbook


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


@pytest.fixture(scope="module")
def canonical():
    contracts = json.loads((DATA_DIR / "contracts.json").read_text(encoding="utf-8"))
    playbooks = json.loads((DATA_DIR / "playbook.json").read_text(encoding="utf-8"))
    contract = next(item for item in contracts if item["document_id"] == "NDA-003")
    playbook = apply_playbook.select_playbook(contract, playbooks)
    return contract, playbook


def finding(issue="unlimited_liability", clause_type="liability"):
    return {
        "finding_id": "NDA-003-F-001",
        "candidate_issue": issue,
        "clause_type": clause_type,
    }


def test_severity_and_rule_id_come_from_playbook(canonical):
    contract, playbook = canonical
    candidate = finding()
    enriched = apply_playbook.apply_playbook(
        [candidate],
        contract=contract,
        playbook=playbook,
    )
    assert enriched[0]["rule_id"] == "MNDA-LIABILITY-001"
    assert enriched[0]["severity"] == "high"
    assert "severity" not in candidate
    assert "route" not in enriched[0]


@pytest.mark.parametrize("field", ["severity", "route"])
def test_candidate_cannot_supply_enforcement_fields(field, canonical):
    contract, playbook = canonical
    candidate = finding()
    candidate[field] = "high"
    with pytest.raises(apply_playbook.PlaybookMatchError, match=field):
        apply_playbook.apply_playbook(
            [candidate],
            contract=contract,
            playbook=playbook,
        )


def test_unmatched_issue_remains_visible_but_unenforced(canonical):
    contract, playbook = canonical
    enriched = apply_playbook.apply_playbook(
        [finding("liability_cap_changed")],
        contract=contract,
        playbook=playbook,
    )
    assert enriched[0]["rule_id"] is None
    assert enriched[0]["severity"] is None


def test_prompt_injection_telemetry_is_not_assigned_legal_severity(canonical):
    contract, playbook = canonical
    enriched = apply_playbook.apply_playbook(
        [finding("prompt_injection_detected", "general")],
        contract=contract,
        playbook=playbook,
    )
    assert enriched[0]["rule_id"] is None
    assert enriched[0]["severity"] is None


def test_union_records_both_analysis_paths(canonical):
    contract, playbook = canonical
    ai = apply_playbook.apply_playbook(
        [finding()],
        contract=contract,
        playbook=playbook,
    )
    baseline_candidate = copy.deepcopy(finding())
    baseline_candidate["finding_id"] = "NDA-003-B-F-001"
    baseline = apply_playbook.apply_playbook(
        [baseline_candidate],
        contract=contract,
        playbook=playbook,
    )
    union = apply_playbook.union_findings(ai, baseline)
    assert union == [
        {
            "candidate_issue": "unlimited_liability",
            "rule_id": "MNDA-LIABILITY-001",
            "severity": "high",
            "analysis_paths": ["ai", "baseline"],
            "finding_ids": ["NDA-003-F-001", "NDA-003-B-F-001"],
        }
    ]
