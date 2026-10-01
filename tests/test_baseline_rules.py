"""Tests for the deterministic baseline pipeline."""
import json
from pathlib import Path

import pytest

from conftest import load_json, validate

import baseline_rules

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


@pytest.fixture(scope="module")
def contracts():
    return {doc["document_id"]: doc for doc in load_json(DATA_DIR / "contracts.json")}


@pytest.fixture(scope="module")
def templates():
    return load_json(DATA_DIR / "templates.json")


@pytest.fixture(scope="module")
def outputs():
    return {o["document_id"]: o for o in baseline_rules.run_baseline()}


def issues(output):
    return {finding["candidate_issue"] for finding in output["findings"]}


def test_rule_set_stays_small():
    assert len(baseline_rules.KEYWORD_RULES) <= 10
    assert len(baseline_rules.KEYWORD_RULES) + len(baseline_rules.STRUCTURAL_RULES) <= 12


@pytest.mark.parametrize("doc_id", ["NDA-003", "NDA-006"])
def test_unlimited_liability_tripwire_fires(doc_id, outputs):
    assert "unlimited_liability" in issues(outputs[doc_id])


@pytest.mark.parametrize("doc_id", ["NDA-001", "NDA-002", "NDA-004", "NDA-005", "NDA-007"])
def test_unlimited_liability_does_not_fire_on_clean_docs(doc_id, outputs):
    assert "unlimited_liability" not in issues(outputs[doc_id])


def test_nda_008_is_the_designed_baseline_miss(outputs):
    # NDA-008 reintroduces unlimited liability in wording none of the
    # baseline liability patterns match, so the baseline must find nothing —
    # only the AI path can catch it. The expected label is unchanged.
    assert issues(outputs["NDA-008"]) == set()


def test_structural_check_flags_missing_termination_in_nda_005(outputs):
    assert "missing_termination_clause" in issues(outputs["NDA-005"])


def test_structural_check_does_not_flag_reworded_nda_002(outputs):
    # NDA-002 expresses the same clauses in different words ("Ending the
    # agreement early"); the structural check must not false-alarm on it.
    assert issues(outputs["NDA-002"]) == set()


def test_injection_does_not_stop_the_tripwire(outputs):
    found = issues(outputs["NDA-006"])
    assert "prompt_injection_detected" in found
    assert "unlimited_liability" in found


def test_order_001_keyword_rules(outputs):
    found = issues(outputs["ORDER-001"])
    assert "payment_terms_extended" in found
    assert "auto_renewal_extended" in found


def test_governing_law_rule_fires_on_nda_004(outputs):
    assert "governing_law_changed" in issues(outputs["NDA-004"])


def test_every_evidence_quote_exists_verbatim(outputs, contracts, templates):
    template_texts = {
        clause["template_clause_id"]: clause["text"]
        for template in templates
        for clause in template["clauses"]
    }
    for doc_id, output in outputs.items():
        for finding in output["findings"]:
            assert len(finding["evidence"]) >= 1
            for evidence in finding["evidence"]:
                if evidence["source"] == "contract":
                    source_text = contracts[evidence["source_id"]]["text"]
                else:
                    source_text = template_texts[evidence["source_id"]]
                start = evidence["start_char"]
                end = evidence["end_char"]
                assert source_text[start:end] == evidence["quote"]
                assert evidence["quote"] in source_text


def test_no_severity_or_route_anywhere(outputs):
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in ("severity", "route", "approval_status")
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for output in outputs.values():
        walk(output)


def test_committed_outputs_match_fresh_run(outputs):
    committed = {
        o["document_id"]: o
        for o in load_json(DATA_DIR / "baseline_outputs.json")
    }
    assert committed.keys() == outputs.keys()
    for doc_id, fresh in outputs.items():
        stored = dict(committed[doc_id])
        # Audit timestamps and wall time legitimately differ between runs.
        fresh = {k: v for k, v in fresh.items() if k != "audit"}
        stored = {k: v for k, v in stored.items() if k != "audit"}
        assert stored == fresh


def test_baseline_outputs_pass_the_shared_schema(outputs, schemas, registry):
    for output in outputs.values():
        validate(output, schemas["findings_v1.schema.json"], registry)


def test_committed_baseline_outputs_pass_the_shared_schema(schemas, registry):
    committed = load_json(DATA_DIR / "baseline_outputs.json")
    for output in committed:
        validate(output, schemas["findings_v1.schema.json"], registry)


def test_baseline_outputs_pass_the_full_shared_validator(outputs, contracts, templates):
    """The shared validator, not just the schema: evidence spans, known IDs,
    deterministic template/playbook selection, no forbidden fields."""
    import validate_output as shared

    playbooks = load_json(DATA_DIR / "playbook.json")
    templates_by_type = {t["contract_type"]: t for t in templates}
    playbooks_by_type = {p["contract_type"]: p for p in playbooks}
    for doc_id, output in outputs.items():
        contract = contracts[doc_id]
        template = templates_by_type.get(contract["contract_type"])
        playbook = playbooks_by_type.get(contract["contract_type"])
        context = shared.ValidationContext(
            contract=contract,
            templates=tuple(templates),
            playbooks=tuple(playbooks),
            expected_analysis_path="baseline",
            expected_template_id=template["template_id"] if template else None,
            expected_playbook_id=playbook["playbook_id"] if playbook else None,
        )
        shared.validate_output(output, context)


def test_unknown_document_gets_null_references(outputs):
    output = outputs["UNKNOWN-001"]
    assert output["template_id"] is None
    assert output["playbook_id"] is None
    assert output["findings"] == []
    assert output["confidence"] < 0.5
