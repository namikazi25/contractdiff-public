#!/usr/bin/env python3
"""Generate validated AI candidate findings for the frozen dataset."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from llm_client import (
    ConfigError,
    LLMConfig,
    LLMError,
    LLMResult,
    MissingConfigError,
    chat_completion,
    load_config,
)
from validate_output import (
    OutputValidationError,
    ValidationContext,
    parse_json_output,
    validate_output,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_PATH = DATA_DIR / "model_outputs.json"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "findings_v1.schema.json"

SCHEMA_VERSION = "findings.v1"
ANALYSIS_PATH = "ai"
MAX_ATTEMPTS = 2

NO_KEY_MESSAGE = (
    "No API key is configured, so live AI generation was skipped. "
    "The offline demo works from the precomputed outputs in "
    "data/model_outputs.json."
)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def select_record(
    records: list[dict[str, Any]], contract: dict[str, Any], id_field: str
) -> dict[str, Any] | None:
    """Select the single active template or playbook for a canonical contract."""

    if contract["contract_type"] == "unknown":
        return None
    matches = [
        record
        for record in records
        if record.get("contract_type") == contract["contract_type"]
        and record.get("jurisdiction") == contract["jurisdiction"]
        and record.get("status") == "active"
    ]
    if len(matches) != 1:
        identifiers = [record.get(id_field) for record in matches]
        raise RuntimeError(
            f"{contract['document_id']}: expected one active {id_field}, "
            f"found {identifiers}"
        )
    return matches[0]


def model_response_schema(
    *,
    contract: dict[str, Any],
    template: dict[str, Any] | None,
    playbook: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the findings schema with pipeline-owned audit metadata omitted."""

    schema = copy.deepcopy(load_json(SCHEMA_PATH))
    schema["title"] = "ContractDiff model response v1"
    schema["description"] = (
        "Candidate response only. The pipeline adds audit metadata after the call."
    )
    schema["required"].remove("audit")
    del schema["properties"]["audit"]
    schema["properties"]["document_id"] = {"const": contract["document_id"]}
    schema["properties"]["analysis_path"] = {"const": ANALYSIS_PATH}
    schema["properties"]["status"] = {"const": "completed"}
    schema["properties"]["template_id"] = {
        "const": None if template is None else template["template_id"]
    }
    schema["properties"]["playbook_id"] = {
        "const": None if playbook is None else playbook["playbook_id"]
    }
    return schema


def build_messages(
    contract: dict[str, Any],
    template: dict[str, Any] | None,
    playbook: dict[str, Any] | None,
    prompt_module: Any,
) -> list[dict[str, str]]:
    metadata = {key: value for key, value in contract.items() if key != "text"}
    user_prompt = prompt_module.render_user_prompt(
        metadata=json.dumps(metadata, ensure_ascii=False, indent=2),
        template=json.dumps(template, ensure_ascii=False, indent=2),
        playbook_rules=json.dumps(
            [] if playbook is None else playbook["rules"],
            ensure_ascii=False,
            indent=2,
        ),
        contract_text=contract["text"],
        schema_summary=json.dumps(
            model_response_schema(
                contract=contract,
                template=template,
                playbook=playbook,
            ),
            ensure_ascii=False,
            indent=2,
        ),
    )
    return [
        {"role": "system", "content": prompt_module.render_system_prompt()},
        {"role": "user", "content": user_prompt},
    ]


def _normalise_usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    values = {key: usage.get(key) for key in keys}
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in values.values()
    ):
        return None
    return values


def _aggregate_usage(results: list[LLMResult]) -> dict[str, int] | None:
    usages = [_normalise_usage(result.usage) for result in results]
    if not results or any(usage is None for usage in usages):
        return None
    return {
        key: sum(usage[key] for usage in usages if usage is not None)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def _aggregate_cost(results: list[LLMResult]) -> float | None:
    costs = [result.cost_usd for result in results]
    # A partial sum would understate a retried run. Keep the total unknown if
    # any attempt omits cost rather than presenting a known-incomplete value.
    if not results or any(
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(cost)
        or cost < 0
        for cost in costs
    ):
        return None
    return float(sum(costs))


def _timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_audit(
    *,
    contract: dict[str, Any],
    template: dict[str, Any] | None,
    config: LLMConfig,
    results: list[LLMResult],
    prompt_version: str,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    returned_models = [result.model for result in results if result.model]
    return {
        "document_id": contract["document_id"],
        "analysis_path": ANALYSIS_PATH,
        "model": returned_models[-1] if returned_models else config.model,
        "prompt_version": prompt_version,
        "schema_version": SCHEMA_VERSION,
        "playbook_version": None,
        "template_version": None if template is None else template["version"],
        "generated_at": _timestamp(now),
        "wall_time_ms": sum(result.wall_time_ms for result in results),
        "usage": _aggregate_usage(results),
        "cost_usd": _aggregate_cost(results),
    }


def validation_context(
    *,
    contract: dict[str, Any],
    templates: list[dict[str, Any]],
    playbooks: list[dict[str, Any]],
    template: dict[str, Any] | None,
    playbook: dict[str, Any] | None,
) -> ValidationContext:
    return ValidationContext(
        contract=contract,
        templates=tuple(templates),
        playbooks=tuple(playbooks),
        expected_analysis_path=ANALYSIS_PATH,
        expected_template_id=None if template is None else template["template_id"],
        expected_playbook_id=None if playbook is None else playbook["playbook_id"],
    )


HEADING_RE = re.compile(r"^\s*(\d+)\.\s+\S", re.MULTILINE)


def _heading_number_before(text: str, offset: int) -> int | None:
    """Return the number of the last numbered heading above ``offset``."""
    number = None
    for match in HEADING_RE.finditer(text):
        if match.start() > offset:
            break
        number = int(match.group(1))
    return number


def _template_clauses_for(
    output: dict[str, Any], context: ValidationContext
) -> dict[str, Any]:
    selected = {
        template.get("template_id"): template for template in context.templates
    }.get(output.get("template_id"), {})
    return {
        clause.get("template_clause_id"): clause
        for clause in selected.get("clauses", [])
    }


def _set_span(evidence: dict[str, Any], source_text: str, quote: str) -> None:
    start = source_text.find(quote)
    evidence["start_char"] = start
    evidence["end_char"] = start + len(quote)


def _normalise_template_clause_id(
    finding: dict[str, Any], clauses: dict[str, Any]
) -> None:
    """Repair hallucinated template clause IDs by clause-type lookup."""
    clause_id = finding.get("template_clause_id")
    if clause_id is None or clause_id in clauses:
        return
    candidates = [
        clause
        for clause in clauses.values()
        if clause.get("clause_type") == finding.get("clause_type")
    ]
    finding["template_clause_id"] = (
        candidates[0]["template_clause_id"] if len(candidates) == 1 else None
    )


def _normalise_evidence(
    finding: dict[str, Any], contract_text: str, clauses: dict[str, Any]
) -> None:
    """Resolve each evidence item to a verbatim source and real span.

    The model's job is the quote; sources and spans are mechanics. A quote
    labelled as contract text that only appears in a template clause is
    re-anchored to that clause. Quotes found nowhere are left untouched so
    validation fails them honestly.
    """
    items = finding.get("evidence")
    if not isinstance(items, list):
        return
    for evidence in items:
        if not isinstance(evidence, dict):
            continue
        quote = evidence.get("quote")
        if not isinstance(quote, str) or not quote:
            continue
        if evidence.get("source") == "contract" and quote in contract_text:
            _set_span(evidence, contract_text, quote)
            continue
        named = clauses.get(evidence.get("source_id"))
        if evidence.get("source") == "template" and named is not None:
            if quote in named.get("text", ""):
                _set_span(evidence, named["text"], quote)
                continue
        preferred = clauses.get(finding.get("template_clause_id"))
        ordered = [preferred] if preferred is not None else []
        ordered.extend(
            clause for clause in clauses.values() if clause is not preferred
        )
        for clause in ordered:
            if quote in clause.get("text", ""):
                evidence["source"] = "template"
                evidence["source_id"] = clause["template_clause_id"]
                _set_span(evidence, clause["text"], quote)
                break


def _normalise_clause_id(
    finding: dict[str, Any], document_id: str, contract_text: str
) -> None:
    """Namespace or re-derive the clause reference."""
    clause_id = finding.get("clause_id")
    if clause_id is None:
        return
    prefix = f"{document_id}-CLAUSE-"
    if isinstance(clause_id, str) and clause_id.startswith(prefix):
        return
    if isinstance(clause_id, str) and clause_id.isdigit():
        finding["clause_id"] = f"{prefix}{int(clause_id):03d}"
        return
    for evidence in finding.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        if evidence.get("source") != "contract":
            continue
        start = evidence.get("start_char")
        if isinstance(start, int) and not isinstance(start, bool):
            number = _heading_number_before(contract_text, start)
            finding["clause_id"] = f"{prefix}{number:03d}" if number else None
            return
    finding["clause_id"] = None


def normalise_model_output(output: dict[str, Any], context: ValidationContext) -> None:
    """Repair mechanical references before validation.

    The model proposes issues, quotes, and reasoning; the pipeline owns
    IDs, source references, and spans. Semantics are never rewritten: a
    quote found in no source still fails validation, and no finding is
    added or removed here.
    """
    clauses = _template_clauses_for(output, context)
    contract_text = context.contract.get("text", "")
    document_id = context.contract.get("document_id", "")
    findings = output.get("findings")
    if not isinstance(findings, list):
        return
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        _normalise_template_clause_id(finding, clauses)
        _normalise_evidence(finding, contract_text, clauses)
        _normalise_clause_id(finding, document_id, contract_text)


def validate_model_response(
    raw_output: str,
    *,
    audit: dict[str, Any],
    context: ValidationContext,
) -> dict[str, Any]:
    output = parse_json_output(raw_output)
    if "audit" in output:
        raise OutputValidationError(
            ["$.audit: audit metadata is assigned by the pipeline, not the model"]
        )
    normalise_model_output(output, context)
    output["audit"] = audit
    validate_output(output, context)
    return output


def analysis_failed_output(
    *,
    contract: dict[str, Any],
    template: dict[str, Any] | None,
    playbook: dict[str, Any] | None,
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Build the deterministic failure marker consumed by the deterministic router."""

    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": contract["document_id"],
        "analysis_path": ANALYSIS_PATH,
        "status": "analysis_failed",
        "contract_type": contract["contract_type"],
        "template_id": None if template is None else template["template_id"],
        "playbook_id": None if playbook is None else playbook["playbook_id"],
        "findings": [],
        "confidence": 0,
        "audit": audit,
    }


def generate_document(
    *,
    contract: dict[str, Any],
    templates: list[dict[str, Any]],
    playbooks: list[dict[str, Any]],
    config: LLMConfig,
    prompt_module: Any,
    call_model: Callable[..., LLMResult] = chat_completion,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Generate one output, retrying validation once before failing closed."""

    template = select_record(templates, contract, "template_id")
    playbook = select_record(playbooks, contract, "playbook_id")
    context = validation_context(
        contract=contract,
        templates=templates,
        playbooks=playbooks,
        template=template,
        playbook=playbook,
    )
    results: list[LLMResult] = []
    failures: list[str] = []
    try:
        messages = build_messages(contract, template, playbook, prompt_module)
    except ValueError as exc:
        failures.append(str(exc))
        audit = build_audit(
            contract=contract,
            template=template,
            config=config,
            results=results,
            prompt_version=prompt_module.PROMPT_VERSION,
            now=now,
        )
        return (
            analysis_failed_output(
                contract=contract,
                template=template,
                playbook=playbook,
                audit=audit,
            ),
            tuple(failures),
        )

    for attempt in range(MAX_ATTEMPTS):
        try:
            result = call_model(messages, config=config)
        except LLMError as exc:
            failures.append(str(exc))
            break
        results.append(result)
        audit = build_audit(
            contract=contract,
            template=template,
            config=config,
            results=results,
            prompt_version=prompt_module.PROMPT_VERSION,
            now=now,
        )
        try:
            output = validate_model_response(
                result.content,
                audit=audit,
                context=context,
            )
        except OutputValidationError as exc:
            failures.extend(exc.errors)
            if attempt + 1 < MAX_ATTEMPTS:
                logger.info(
                    "%s: output failed validation; retrying once with the errors",
                    contract["document_id"],
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": result.content},
                        {
                            "role": "user",
                            "content": prompt_module.render_retry_prompt(str(exc)),
                        },
                    ]
                )
                continue
            break
        return output, tuple(failures)

    audit = build_audit(
        contract=contract,
        template=template,
        config=config,
        results=results,
        prompt_version=prompt_module.PROMPT_VERSION,
        now=now,
    )
    return (
        analysis_failed_output(
            contract=contract,
            template=template,
            playbook=playbook,
            audit=audit,
        ),
        tuple(failures),
    )


def generate_all(
    *,
    contracts: list[dict[str, Any]],
    templates: list[dict[str, Any]],
    playbooks: list[dict[str, Any]],
    config: LLMConfig,
    prompt_module: Any,
    call_model: Callable[..., LLMResult] = chat_completion,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]]]:
    outputs = []
    failures = {}
    for contract in contracts:
        output, errors = generate_document(
            contract=contract,
            templates=templates,
            playbooks=playbooks,
            config=config,
            prompt_module=prompt_module,
            call_model=call_model,
            now=now,
        )
        outputs.append(output)
        if output["status"] == "analysis_failed":
            failures[contract["document_id"]] = errors
    return outputs, failures


def write_outputs(path: Path, outputs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(outputs, ensure_ascii=False, indent=2) + "\n"
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


def _contexts_for_dataset(
    contracts: list[dict[str, Any]],
    templates: list[dict[str, Any]],
    playbooks: list[dict[str, Any]],
) -> dict[str, ValidationContext]:
    contexts = {}
    for contract in contracts:
        template = select_record(templates, contract, "template_id")
        playbook = select_record(playbooks, contract, "playbook_id")
        contexts[contract["document_id"]] = validation_context(
            contract=contract,
            templates=templates,
            playbooks=playbooks,
            template=template,
            playbook=playbook,
        )
    return contexts


def load_existing_outputs(
    path: Path,
    *,
    contracts: list[dict[str, Any]],
    templates: list[dict[str, Any]],
    playbooks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    outputs = load_json(path)
    if not isinstance(outputs, list):
        raise OutputValidationError(["stored outputs must be a JSON array"])
    contexts = _contexts_for_dataset(contracts, templates, playbooks)
    expected_ids = set(contexts)
    actual_ids = [output.get("document_id") for output in outputs if isinstance(output, dict)]
    if len(actual_ids) != len(outputs):
        raise OutputValidationError(["every stored output must be a JSON object"])
    if len(actual_ids) != len(set(actual_ids)):
        raise OutputValidationError(["stored output document IDs must be unique"])
    if set(actual_ids) != expected_ids:
        raise OutputValidationError(
            ["stored outputs must contain exactly one record for every canonical document"]
        )
    for output in outputs:
        if output.get("status") == "analysis_failed":
            # Failure markers are pipeline-owned. Replay validation permits
            # that status while still checking canonical references and audit.
            validate_output(
                output,
                contexts[output["document_id"]],
                allow_analysis_failed=True,
            )
        else:
            validate_output(output, contexts[output["document_id"]])
    return outputs


def _load_dataset(data_dir: Path):
    return (
        load_json(data_dir / "contracts.json"),
        load_json(data_dir / "templates.json"),
        load_json(data_dir / "playbook.json"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    contracts, templates, playbooks = _load_dataset(args.data_dir)

    try:
        config = load_config()
    except MissingConfigError:
        logger.warning(NO_KEY_MESSAGE)
        if args.output.is_file():
            try:
                outputs = load_existing_outputs(
                    args.output,
                    contracts=contracts,
                    templates=templates,
                    playbooks=playbooks,
                )
            except (OSError, json.JSONDecodeError, OutputValidationError) as exc:
                logger.error("Precomputed outputs are invalid: %s", exc)
                return 1
            logger.info("Loaded and validated %d precomputed outputs.", len(outputs))
        else:
            logger.info("No precomputed output file is present yet; no files were changed.")
        return 0
    except ConfigError as exc:
        logger.error("Invalid LLM configuration: %s", exc)
        return 2

    try:
        import prompt_contract
    except ImportError as exc:
        raise SystemExit(
            "prompt_contract.py is required before live generation."
        ) from exc

    outputs, failures = generate_all(
        contracts=contracts,
        templates=templates,
        playbooks=playbooks,
        config=config,
        prompt_module=prompt_contract,
    )
    write_outputs(args.output, outputs)
    for document_id, errors in failures.items():
        logger.warning("%s: analysis failed validation: %s", document_id, "; ".join(errors))
    logger.info("Wrote %d outputs to %s", len(outputs), args.output)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
