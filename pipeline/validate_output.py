"""Shared candidate-output validation for the AI and baseline paths.

JSON Schema owns the structural boundary.  The checks here resolve claims
against the frozen canonical data: document ownership, template/playbook
references, and verbatim evidence spans.  Playbook enforcement is
deliberately absent; Playbook enforcement is the only layer that may assign severity or
route.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from referencing import Registry, Resource


SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
FINDINGS_SCHEMA = SCHEMA_DIR / "findings_v1.schema.json"

FORBIDDEN_MODEL_FIELDS = {
    "severity",
    "route",
    "approval",
    "approval_status",
    "approved",
    "is_approved",
    "decision",
}

APPROVAL_CLAIM_PATTERNS = (
    re.compile(
        r"\b(?:contract|agreement|document)\s+"
        r"(?:is|should be|has been)\s+(?:approved|rejected)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:approve|reject)\s+(?:the\s+)?"
        r"(?:contract|agreement|document)\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class ValidationContext:
    """Canonical records used to validate one output."""

    contract: dict[str, Any]
    templates: tuple[dict[str, Any], ...]
    playbooks: tuple[dict[str, Any], ...]
    expected_analysis_path: str | None = None
    expected_template_id: str | None = None
    expected_playbook_id: str | None = None


class OutputValidationError(ValueError):
    """Raised with all useful validation failures from one candidate."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(dict.fromkeys(errors))
        super().__init__("\n".join(f"- {error}" for error in self.errors))


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _build_validator():
    schemas = {
        path.name: _load_json(path)
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    )
    schema = schemas[FINDINGS_SCHEMA.name]
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )


_VALIDATOR = _build_validator()


def parse_json_output(raw_output: str) -> dict[str, Any]:
    """Parse one raw model response, rejecting non-object JSON."""

    if not isinstance(raw_output, str):
        raise OutputValidationError(["model response content must be a string"])
    try:
        output = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise OutputValidationError(
            [f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]
        ) from exc
    if not isinstance(output, dict):
        raise OutputValidationError(["output must be a JSON object"])
    return output


def _json_path(parts: Iterable[Any]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _schema_errors(output: dict[str, Any]) -> list[str]:
    errors = sorted(
        _VALIDATOR.iter_errors(output),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        f"{_json_path(error.absolute_path)}: {error.message}"
        for error in errors
    ]


def _find_forbidden_fields(value: Any, path: str = "$") -> list[str]:
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_MODEL_FIELDS:
                errors.append(
                    f"{child_path}: field is reserved for deterministic enforcement"
                )
            errors.extend(_find_forbidden_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_find_forbidden_fields(child, f"{path}[{index}]"))
    return errors


def _has_approval_claim(text: str) -> bool:
    return any(pattern.search(text) for pattern in APPROVAL_CLAIM_PATTERNS)


def _validate_evidence(
    evidence: dict[str, Any],
    *,
    path: str,
    contract: dict[str, Any],
    template_clauses: dict[str, dict[str, Any]],
) -> list[str]:
    source = evidence.get("source")
    source_id = evidence.get("source_id")
    quote = evidence.get("quote")
    start = evidence.get("start_char")
    end = evidence.get("end_char")
    if not (
        isinstance(quote, str)
        and isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
    ):
        return []  # The schema reports malformed evidence fields.

    if source == "contract":
        if source_id != contract.get("document_id"):
            return [f"{path}.source_id: must identify the current document"]
        source_text = contract.get("text", "")
    elif source == "template":
        clause = template_clauses.get(source_id)
        if clause is None:
            return [f"{path}.source_id: unknown template clause ID {source_id!r}"]
        source_text = clause.get("text", "")
    else:
        return []  # The schema reports the unknown source.

    errors = []
    if not 0 <= start < end <= len(source_text):
        errors.append(f"{path}: evidence span is outside its source text")
    elif source_text[start:end] != quote:
        errors.append(f"{path}: evidence span does not exactly match its quote")
    if quote not in source_text:
        errors.append(f"{path}.quote: quote does not appear verbatim in its source")
    return errors


def _custom_errors(
    output: dict[str, Any],
    context: ValidationContext,
    *,
    allow_analysis_failed: bool,
) -> list[str]:
    errors = _find_forbidden_fields(output)
    document_id = context.contract.get("document_id")

    if output.get("document_id") != document_id:
        errors.append("$.document_id: does not identify the current document")
    if (
        context.expected_analysis_path is not None
        and output.get("analysis_path") != context.expected_analysis_path
    ):
        errors.append(
            "$.analysis_path: does not match the analysis path being executed"
        )
    status = output.get("status")
    if status != "completed" and not (allow_analysis_failed and status == "analysis_failed"):
        errors.append("$.status: only the pipeline may assign analysis_failed")
    if status == "analysis_failed" and allow_analysis_failed:
        if output.get("findings") != []:
            errors.append("$.findings: an analysis_failed marker must have no findings")
        if output.get("confidence") != 0:
            errors.append("$.confidence: an analysis_failed marker must use zero confidence")

    templates = {
        template.get("template_id"): template for template in context.templates
    }
    playbooks = {
        playbook.get("playbook_id"): playbook for playbook in context.playbooks
    }
    template_id = output.get("template_id")
    playbook_id = output.get("playbook_id")
    if template_id is not None and template_id not in templates:
        errors.append(f"$.template_id: unknown template ID {template_id!r}")
    if playbook_id is not None and playbook_id not in playbooks:
        errors.append(f"$.playbook_id: unknown playbook ID {playbook_id!r}")
    if template_id != context.expected_template_id:
        errors.append("$.template_id: does not match deterministic template selection")
    if playbook_id != context.expected_playbook_id:
        errors.append("$.playbook_id: does not match deterministic playbook selection")

    selected_template = templates.get(template_id, {})
    template_clauses = {
        clause.get("template_clause_id"): clause
        for clause in selected_template.get("clauses", [])
    }
    findings = output.get("findings")
    if isinstance(findings, list):
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                continue
            path = f"$.findings[{index}]"
            if finding.get("document_id") != document_id:
                errors.append(f"{path}.document_id: finding belongs to another document")
            clause_id = finding.get("clause_id")
            if isinstance(clause_id, str) and not clause_id.startswith(
                f"{document_id}-CLAUSE-"
            ):
                errors.append(
                    f"{path}.clause_id: must be namespaced to the current document"
                )
            template_clause_id = finding.get("template_clause_id")
            if (
                template_clause_id is not None
                and template_clause_id not in template_clauses
            ):
                errors.append(
                    f"{path}.template_clause_id: unknown template clause ID "
                    f"{template_clause_id!r}"
                )
            for field in ("summary", "why_it_matters", "suggested_redline"):
                value = finding.get(field)
                if isinstance(value, str) and _has_approval_claim(value):
                    errors.append(f"{path}.{field}: claims a final approval decision")
            evidence_items = finding.get("evidence")
            if isinstance(evidence_items, list):
                for evidence_index, evidence in enumerate(evidence_items):
                    if isinstance(evidence, dict):
                        errors.extend(
                            _validate_evidence(
                                evidence,
                                path=f"{path}.evidence[{evidence_index}]",
                                contract=context.contract,
                                template_clauses=template_clauses,
                            )
                        )

    audit = output.get("audit")
    if isinstance(audit, dict):
        if audit.get("document_id") != document_id:
            errors.append("$.audit.document_id: does not identify the current document")
        if audit.get("analysis_path") != output.get("analysis_path"):
            errors.append("$.audit.analysis_path: does not match the output envelope")
        if audit.get("schema_version") != output.get("schema_version"):
            errors.append("$.audit.schema_version: does not match the output envelope")
        if audit.get("playbook_version") is not None:
            errors.append(
                "$.audit.playbook_version: must be null before playbook enforcement"
            )
        expected_template_version = selected_template.get("version") or None
        if audit.get("template_version") != expected_template_version:
            errors.append(
                "$.audit.template_version: does not match the selected template"
            )
    return errors


def validate_output(
    output: dict[str, Any],
    context: ValidationContext,
    *,
    allow_analysis_failed: bool = False,
) -> None:
    """Validate a completed candidate output or raise OutputValidationError."""

    errors = _schema_errors(output)
    errors.extend(
        _custom_errors(
            output,
            context,
            allow_analysis_failed=allow_analysis_failed,
        )
    )
    if errors:
        raise OutputValidationError(errors)


def validate_schema(output: dict[str, Any]) -> None:
    """Validate only the stored envelope shape, including failure markers."""

    errors = _schema_errors(output)
    if errors:
        raise OutputValidationError(errors)


def validate_output_json(raw_output: str, context: ValidationContext) -> dict[str, Any]:
    """Parse and validate a complete stored output."""

    output = parse_json_output(raw_output)
    validate_output(output, context)
    return output
