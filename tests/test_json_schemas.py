import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "pipeline" / "schemas"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "schemas"


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def schemas():
    return {
        path.name: load_json(path)
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }


@pytest.fixture(scope="module")
def registry(schemas):
    return Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    )


def validate(instance, schema, registry):
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator_class(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(instance)


@pytest.mark.parametrize(
    ("schema_name", "fixture_name"),
    [
        ("findings_v1.schema.json", "valid_ai_output.json"),
        ("analysis_meta_v1.schema.json", "valid_analysis_meta.json"),
        ("evaluation_result_v1.schema.json", "valid_evaluation_result.json"),
    ],
)
def test_valid_examples_pass(schema_name, fixture_name, schemas, registry):
    validate(load_json(FIXTURE_DIR / fixture_name), schemas[schema_name], registry)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "invalid_severity.json",
        "invalid_route.json",
        "invalid_candidate_issue.json",
        "invalid_confidence.json",
    ],
)
def test_invalid_findings_examples_fail(fixture_name, schemas, registry):
    with pytest.raises(ValidationError):
        validate(
            load_json(FIXTURE_DIR / fixture_name),
            schemas["findings_v1.schema.json"],
            registry,
        )


def remove_required_field(instance):
    instance.pop("document_id")


def set_unknown_clause_type(instance):
    instance["findings"][0]["clause_type"] = "force_majeure"


def add_enforcement_field(instance):
    instance["findings"][0]["approval_status"] = "approved"


@pytest.mark.parametrize(
    "mutation",
    [remove_required_field, set_unknown_clause_type, add_enforcement_field],
    ids=["missing-required-field", "unknown-clause-type", "extra-enforcement-field"],
)
def test_other_contract_violations_fail(mutation, schemas, registry):
    instance = deepcopy(load_json(FIXTURE_DIR / "valid_ai_output.json"))
    mutation(instance)

    with pytest.raises(ValidationError):
        validate(instance, schemas["findings_v1.schema.json"], registry)


def test_malformed_json_fails_before_schema_validation():
    with pytest.raises(json.JSONDecodeError):
        load_json(FIXTURE_DIR / "invalid_json.json")
