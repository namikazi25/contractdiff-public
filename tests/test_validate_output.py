"""Custom validation tests."""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from validate_output import (  # noqa: E402
    OutputValidationError,
    ValidationContext,
    parse_json_output,
    validate_output,
)


def load(name):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def canonical():
    contracts = load("contracts.json")
    templates = load("templates.json")
    playbooks = load("playbook.json")
    contract = next(item for item in contracts if item["document_id"] == "NDA-003")
    template = next(item for item in templates if item["template_id"] == "TPL-MNDA-UK-1.3")
    playbook = next(item for item in playbooks if item["playbook_id"] == "PB-MNDA-UK-1.0")
    return contract, templates, playbooks, template, playbook


@pytest.fixture
def valid_output(canonical):
    contract, _, _, template, playbook = canonical
    quote = "Each party accepts unlimited liability under this Agreement."
    start = contract["text"].index(quote)
    return {
        "schema_version": "findings.v1",
        "document_id": contract["document_id"],
        "analysis_path": "ai",
        "status": "completed",
        "contract_type": "mutual_nda",
        "template_id": template["template_id"],
        "playbook_id": playbook["playbook_id"],
        "findings": [
            {
                "finding_id": "NDA-003-F-001",
                "document_id": "NDA-003",
                "clause_id": "NDA-003-CLAUSE-008",
                "clause_type": "liability",
                "candidate_issue": "unlimited_liability",
                "template_clause_id": "TPL-MNDA-LIABILITY-001",
                "evidence": [
                    {
                        "source": "contract",
                        "source_id": "NDA-003",
                        "quote": quote,
                        "start_char": start,
                        "end_char": start + len(quote),
                    }
                ],
                "summary": "The draft replaces the liability cap with unlimited liability.",
                "why_it_matters": "The change creates uncapped exposure.",
                "suggested_redline": "Restore the mutual liability cap.",
                "confidence": 0.98,
            }
        ],
        "confidence": 0.97,
        "audit": {
            "document_id": "NDA-003",
            "analysis_path": "ai",
            "model": "test-model",
            "prompt_version": "contract-review.v1",
            "schema_version": "findings.v1",
            "playbook_version": None,
            "template_version": "1.3",
            "generated_at": "2026-08-26T12:00:00Z",
            "wall_time_ms": 10,
            "usage": None,
            "cost_usd": None,
        },
    }


def context(canonical):
    contract, templates, playbooks, template, playbook = canonical
    return ValidationContext(
        contract=contract,
        templates=tuple(templates),
        playbooks=tuple(playbooks),
        expected_analysis_path="ai",
        expected_template_id=template["template_id"],
        expected_playbook_id=playbook["playbook_id"],
    )


def test_valid_ai_output_passes(valid_output, canonical):
    validate_output(valid_output, context(canonical))


def test_invalid_json_fails():
    with pytest.raises(OutputValidationError, match="invalid JSON"):
        parse_json_output('{"findings": [}')


@pytest.mark.parametrize("field,value", [("severity", "high"), ("route", "quick_check")])
def test_model_enforcement_fields_are_rejected(field, value, valid_output, canonical):
    invalid = copy.deepcopy(valid_output)
    invalid["findings"][0][field] = value
    with pytest.raises(OutputValidationError, match=field):
        validate_output(invalid, context(canonical))


def test_model_approval_claim_is_rejected(valid_output, canonical):
    invalid = copy.deepcopy(valid_output)
    invalid["findings"][0]["summary"] = "The contract should be approved."
    with pytest.raises(OutputValidationError, match="approval decision"):
        validate_output(invalid, context(canonical))


def test_evidence_must_match_source_span(valid_output, canonical):
    invalid = copy.deepcopy(valid_output)
    invalid["findings"][0]["evidence"][0]["start_char"] += 1
    with pytest.raises(OutputValidationError, match="evidence span"):
        validate_output(invalid, context(canonical))


def test_finding_must_belong_to_current_document(valid_output, canonical):
    invalid = copy.deepcopy(valid_output)
    invalid["findings"][0]["document_id"] = "NDA-004"
    with pytest.raises(OutputValidationError, match="another document"):
        validate_output(invalid, context(canonical))


@pytest.mark.parametrize(
    "filename,analysis_path",
    [("model_outputs.json", "ai"), ("baseline_outputs.json", "baseline")],
)
def test_committed_candidates_pass_full_validation(filename, analysis_path):
    """The public replay must preserve evidence spans and policy boundaries."""
    contracts = {doc["document_id"]: doc for doc in load("contracts.json")}
    templates = load("templates.json")
    playbooks = load("playbook.json")
    outputs = load(filename)
    assert {item["document_id"] for item in outputs} == set(contracts)
    assert len(outputs) == len(contracts)
    for output in outputs:
        contract = contracts[output["document_id"]]
        template = next(
            (item for item in templates
             if item["contract_type"] == contract["contract_type"]), None
        )
        playbook = next(
            (item for item in playbooks
             if item["contract_type"] == contract["contract_type"]), None
        )
        context = ValidationContext(
            contract=contract,
            templates=tuple(templates),
            playbooks=tuple(playbooks),
            expected_analysis_path=analysis_path,
            expected_template_id=template["template_id"] if template else None,
            expected_playbook_id=playbook["playbook_id"] if playbook else None,
        )
        validate_output(output, context, allow_analysis_failed=True)
