"""Shared pytest fixtures: pipeline imports and the local schema registry."""
import json
import sys
from pathlib import Path

import pytest
from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

SCHEMA_DIR = ROOT / "pipeline" / "schemas"


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def schemas():
    return {
        path.name: load_json(path)
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }


@pytest.fixture(scope="session")
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
