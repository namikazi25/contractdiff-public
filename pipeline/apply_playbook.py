"""Deterministically enrich candidate findings from the active playbook.

Candidate outputs from the AI and baseline paths contain no severity or route.  This
module is the first enforcement boundary: it resolves each legal issue to a
known playbook rule and copies only that rule's severity into routing data.
"""
from __future__ import annotations

import copy
from typing import Any


class PlaybookMatchError(ValueError):
    """Raised when candidate findings cannot be enforced unambiguously."""


def select_playbook(
    contract: dict[str, Any],
    playbooks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the single active canonical playbook for a supported contract."""

    if contract["contract_type"] == "unknown":
        return None
    matches = [
        playbook
        for playbook in playbooks
        if playbook.get("contract_type") == contract["contract_type"]
        and playbook.get("jurisdiction") == contract["jurisdiction"]
        and playbook.get("status") == "active"
    ]
    if len(matches) != 1:
        identifiers = [playbook.get("playbook_id") for playbook in matches]
        raise PlaybookMatchError(
            f"{contract['document_id']}: expected one active playbook, "
            f"found {identifiers}"
        )
    return matches[0]


def _rules_by_issue(playbook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rules = {}
    for rule in playbook.get("rules", []):
        issue = rule.get("candidate_issue")
        if issue in rules:
            raise PlaybookMatchError(
                f"{playbook['playbook_id']}: candidate issue {issue!r} "
                "matches more than one rule"
            )
        rules[issue] = rule
    return rules


def apply_playbook(
    findings: list[dict[str, Any]],
    *,
    contract: dict[str, Any],
    playbook: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Copy rule ID and severity onto findings without mutating candidates."""

    rules = {} if playbook is None else _rules_by_issue(playbook)
    enriched = []
    for index, finding in enumerate(findings):
        if "severity" in finding or "route" in finding:
            raise PlaybookMatchError(
                f"finding {index}: candidate supplied severity or route"
            )
        issue = finding.get("candidate_issue")
        item = copy.deepcopy(finding)
        rule = rules.get(issue)
        if rule is None:
            # uncovered issues remain visible operationally but cannot
            # acquire legal severity or affect routing without a playbook rule.
            item["rule_id"] = None
            item["severity"] = None
            enriched.append(item)
            continue
        if rule.get("contract_type") != contract["contract_type"]:
            raise PlaybookMatchError(
                f"{rule['rule_id']}: rule contract type does not match "
                f"{contract['document_id']}"
            )
        rule_clause_type = rule.get("clause_type")
        if (
            rule_clause_type != "general"
            and finding.get("clause_type") != rule_clause_type
        ):
            raise PlaybookMatchError(
                f"{rule['rule_id']}: clause type does not match finding {index}"
            )
        item["rule_id"] = rule["rule_id"]
        item["severity"] = rule["severity"]
        enriched.append(item)
    return enriched


def union_findings(
    ai_findings: list[dict[str, Any]],
    baseline_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one auditable issue record for the union of both paths."""

    grouped: dict[str, dict[str, Any]] = {}
    for analysis_path, findings in (
        ("ai", ai_findings),
        ("baseline", baseline_findings),
    ):
        for finding in findings:
            issue = finding["candidate_issue"]
            existing = grouped.get(issue)
            if existing is None:
                grouped[issue] = {
                    "candidate_issue": issue,
                    "rule_id": finding["rule_id"],
                    "severity": finding["severity"],
                    "analysis_paths": [analysis_path],
                    "finding_ids": [finding["finding_id"]],
                }
                continue
            if (
                existing["rule_id"] != finding["rule_id"]
                or existing["severity"] != finding["severity"]
            ):
                raise PlaybookMatchError(
                    f"paths disagree on enforcement for candidate issue {issue!r}"
                )
            if analysis_path not in existing["analysis_paths"]:
                existing["analysis_paths"].append(analysis_path)
            existing["finding_ids"].append(finding["finding_id"])
    return list(grouped.values())
