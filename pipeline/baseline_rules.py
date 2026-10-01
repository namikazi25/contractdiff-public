#!/usr/bin/env python3
"""Deterministic baseline pipeline.

A small set of keyword/regex rules plus structural checks that produces the
same findings.v1 candidate output shape as the AI path. It is boring on
purpose: a comparison point for the evaluation and a tripwire
for prompt injection. If the model is steered into silence, this path
still catches obvious high-risk phrases.

The baseline proposes candidate findings only. It never assigns severity or
route; that is the deterministic policy layer's job. Running it writes
``data/baseline_outputs.json``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

CONFIDENCE_KEYWORD_MATCH = 0.95
CONFIDENCE_STRUCTURAL = 0.9
CONFIDENCE_NO_FINDINGS = 0.9
CONFIDENCE_UNSUPPORTED = 0.3

MODEL_ID = "deterministic-baseline"

HEADING_RE = re.compile(r"^\s*(\d+)\.\s+(\S.*)$", re.MULTILINE)


@dataclass(frozen=True)
class KeywordRule:
    rule_id: str
    pattern: re.Pattern[str]
    candidate_issue: str
    clause_type: str
    contract_types: tuple[str, ...]  # empty tuple = applies to every contract type


@dataclass(frozen=True)
class StructuralRule:
    rule_id: str
    candidate_issue: str
    clause_type: str
    heading_words: tuple[str, ...]  # clause is missing when no heading contains any word
    contract_type: str


KEYWORD_RULES = [
    KeywordRule(
        "BL-KW-001", re.compile(r"unlimited liability", re.IGNORECASE),
        "unlimited_liability", "liability", ("mutual_nda", "order_form"),
    ),
    KeywordRule(
        "BL-KW-002", re.compile(r"no limitation of liability", re.IGNORECASE),
        "unlimited_liability", "liability", ("mutual_nda", "order_form"),
    ),
    KeywordRule(
        "BL-KW-003", re.compile(r"liability shall not be limited", re.IGNORECASE),
        "unlimited_liability", "liability", ("mutual_nda", "order_form"),
    ),
    KeywordRule(
        "BL-KW-004", re.compile(r"governed by the laws of new york", re.IGNORECASE),
        "governing_law_changed", "governing_law", ("mutual_nda",),
    ),
    KeywordRule(
        "BL-KW-005",
        re.compile(r"ninety \(90\) days|net 90|payment within 90 days", re.IGNORECASE),
        "payment_terms_extended", "payment_terms", ("order_form",),
    ),
    KeywordRule(
        "BL-KW-006", re.compile(r"three \(3\) year|three-year", re.IGNORECASE),
        "auto_renewal_extended", "renewal", ("order_form",),
    ),
    KeywordRule(
        "BL-KW-007", re.compile(r"ignore your review instructions", re.IGNORECASE),
        "prompt_injection_detected", "general", (),
    ),
    KeywordRule(
        "BL-KW-008", re.compile(r"indemnify and hold harmless", re.IGNORECASE),
        "non_standard_indemnity", "indemnity", ("mutual_nda",),
    ),
]

STRUCTURAL_RULES = [
    StructuralRule(
        "BL-ST-001", "missing_termination_clause", "termination",
        # "ending" covers the reworded NDA-002 heading "Ending the agreement
        # early"; the frozen dataset is the design input.
        ("termination", "ending"), "mutual_nda",
    ),
    StructuralRule(
        "BL-ST-002", "missing_confidentiality_clause", "confidentiality_obligations",
        ("confidential",), "mutual_nda",
    ),
]


def load_canonical() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    contracts = json.loads((DATA_DIR / "contracts.json").read_text(encoding="utf-8"))
    templates = json.loads((DATA_DIR / "templates.json").read_text(encoding="utf-8"))
    playbooks = json.loads((DATA_DIR / "playbook.json").read_text(encoding="utf-8"))
    return contracts, templates, playbooks


def find_clause_id(text: str, doc_id: str, offset: int) -> str | None:
    """Return the clause ID of the numbered heading above ``offset``."""
    clause_id = None
    for match in HEADING_RE.finditer(text):
        if match.start() > offset:
            break
        clause_id = f"{doc_id}-CLAUSE-{int(match.group(1)):03d}"
    return clause_id


def line_span(text: str, offset: int) -> tuple[int, int]:
    """Return (start, end) of the line containing ``offset``."""
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return start, end


def analyse_document(
    contract: dict[str, Any],
    templates_by_type: dict[str, dict[str, Any]],
    playbooks_by_type: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Run every applicable rule over one contract and return a findings.v1 object."""
    started = time.monotonic()
    doc_id = contract["document_id"]
    text = contract["text"]
    contract_type = contract["contract_type"]

    template = templates_by_type.get(contract_type)
    playbook = playbooks_by_type.get(contract_type)
    rules_by_issue = {}
    if playbook:
        rules_by_issue = {
            rule["candidate_issue"]: rule for rule in playbook["rules"]
        }
    template_clause_by_type = {}
    if template:
        template_clause_by_type = {
            clause["clause_type"]: clause for clause in template["clauses"]
        }

    findings = []
    seen_issues = set()

    def template_clause_id_for(clause_type):
        clause = template_clause_by_type.get(clause_type)
        return clause["template_clause_id"] if clause else None

    def playbook_context(issue):
        rule = rules_by_issue.get(issue)
        if rule is None:
            return (
                "No playbook rule covers this issue.",
                None,
            )
        return (
            f"Playbook rule {rule['rule_id']} position: "
            f"{rule['preferred_position']}.",
            rule["suggested_fallback_language"],
        )

    for rule in KEYWORD_RULES:
        if rule.contract_types and contract_type not in rule.contract_types:
            continue
        if rule.candidate_issue in seen_issues:
            continue
        match = rule.pattern.search(text)
        if match is None:
            continue
        seen_issues.add(rule.candidate_issue)
        start, end = line_span(text, match.start())
        why, redline = playbook_context(rule.candidate_issue)
        findings.append({
            "finding_id": f"{doc_id}-F-{len(findings) + 1:03d}",
            "document_id": doc_id,
            "clause_id": find_clause_id(text, doc_id, match.start()),
            "clause_type": rule.clause_type,
            "candidate_issue": rule.candidate_issue,
            "template_clause_id": template_clause_id_for(rule.clause_type),
            "evidence": [{
                "source": "contract",
                "source_id": doc_id,
                "quote": text[start:end],
                "start_char": start,
                "end_char": end,
            }],
            "summary": (
                f"Baseline rule {rule.rule_id} matched "
                f"'{match.group(0)}' in the contract text."
            ),
            "why_it_matters": why,
            "suggested_redline": redline,
            "confidence": CONFIDENCE_KEYWORD_MATCH,
        })

    headings = [match.group(2) for match in HEADING_RE.finditer(text)]
    for rule in STRUCTURAL_RULES:
        if contract_type != rule.contract_type:
            continue
        if rule.candidate_issue in seen_issues:
            continue
        present = any(
            word in heading.lower()
            for heading in headings
            for word in rule.heading_words
        )
        if present:
            continue
        seen_issues.add(rule.candidate_issue)
        clause = template_clause_by_type.get(rule.clause_type)
        if clause is None:
            continue  # nothing to compare against; not a supported check
        why, redline = playbook_context(rule.candidate_issue)
        findings.append({
            "finding_id": f"{doc_id}-F-{len(findings) + 1:03d}",
            "document_id": doc_id,
            "clause_id": None,
            "clause_type": rule.clause_type,
            "candidate_issue": rule.candidate_issue,
            "template_clause_id": clause["template_clause_id"],
            "evidence": [{
                "source": "template",
                "source_id": clause["template_clause_id"],
                "quote": clause["text"],
                "start_char": 0,
                "end_char": len(clause["text"]),
            }],
            "summary": (
                f"Baseline structural check {rule.rule_id}: no clause "
                f"heading containing {list(rule.heading_words)} was found "
                "in the contract."
            ),
            "why_it_matters": why,
            "suggested_redline": redline,
            "confidence": CONFIDENCE_STRUCTURAL,
        })

    if template is None:
        confidence = CONFIDENCE_UNSUPPORTED
    elif findings:
        confidence = min(finding["confidence"] for finding in findings)
    else:
        confidence = CONFIDENCE_NO_FINDINGS

    wall_time_ms = round((time.monotonic() - started) * 1000)
    return {
        "schema_version": "findings.v1",
        "document_id": doc_id,
        "analysis_path": "baseline",
        "status": "completed",
        "contract_type": contract_type,
        "template_id": template["template_id"] if template else None,
        "playbook_id": playbook["playbook_id"] if playbook else None,
        "findings": findings,
        "confidence": confidence,
        "audit": {
            "document_id": doc_id,
            "analysis_path": "baseline",
            "model": MODEL_ID,
            "prompt_version": None,
            "schema_version": "findings.v1",
            "playbook_version": None,
            "template_version": template["version"] if template else None,
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "wall_time_ms": wall_time_ms,
            "usage": None,
            "cost_usd": None,
        },
    }


def run_baseline(
    contracts: list[dict[str, Any]] | None = None,
    templates: list[dict[str, Any]] | None = None,
    playbooks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Analyse every frozen contract and return the list of output objects."""
    if contracts is None:
        contracts, templates, playbooks = load_canonical()
    templates_by_type = {tpl["contract_type"]: tpl for tpl in templates}
    playbooks_by_type = {pb["contract_type"]: pb for pb in playbooks}
    return [
        analyse_document(contract, templates_by_type, playbooks_by_type)
        for contract in contracts
    ]


def validate_outputs(outputs, contracts, templates, playbooks):
    """Fail closed: every output must pass the shared validator."""
    from validate_output import ValidationContext, validate_output

    contracts_by_id = {c["document_id"]: c for c in contracts}
    templates_by_type = {tpl["contract_type"]: tpl for tpl in templates}
    playbooks_by_type = {pb["contract_type"]: pb for pb in playbooks}
    for output in outputs:
        contract = contracts_by_id[output["document_id"]]
        template = templates_by_type.get(contract["contract_type"])
        playbook = playbooks_by_type.get(contract["contract_type"])
        context = ValidationContext(
            contract=contract,
            templates=tuple(templates),
            playbooks=tuple(playbooks),
            expected_analysis_path="baseline",
            expected_template_id=template["template_id"] if template else None,
            expected_playbook_id=playbook["playbook_id"] if playbook else None,
        )
        validate_output(output, context)


def main() -> None:
    contracts, templates, playbooks = load_canonical()
    outputs = run_baseline(contracts, templates, playbooks)
    validate_outputs(outputs, contracts, templates, playbooks)
    out_file = DATA_DIR / "baseline_outputs.json"
    out_file.write_text(
        json.dumps(outputs, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    total = sum(len(output["findings"]) for output in outputs)
    logger.info("wrote %s (%d documents, %d findings)", out_file, len(outputs), total)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
