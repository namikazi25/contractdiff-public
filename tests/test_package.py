"""Build and validate the clean, self-contained public demo archive.

Running this module directly builds ``dist/contractdiff-demo.zip``.
The test suite inspects that same archive, including the nested DOCX XML.
"""
import hashlib
import io
import json
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ZIP_PATH = DIST / "contractdiff-demo.zip"
PREFIX = "contractdiff-demo/"

# Allowlisted source and generated demo data only. Local configuration and
# working material are excluded even if present beside this public export.
SHIP_FILES = [
    "README.md",
    "EVALUATION.md",
    "SECURITY.md",
    ".gitignore",
    ".env.example",
    "requirements-dev.txt",
    "redline/nda_003_redline.docx",
]
SHIP_TREES = ["prototype", "pipeline", "data", "evaluation", "tests", ".github"]
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store", "results_run"}


def _tree_files(tree: str):
    for path in sorted((ROOT / tree).rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in EXCLUDE_NAMES for part in path.parts):
            continue
        if path.name.startswith("~$") or path.suffix == ".pyc":
            continue
        yield path


def build_zip() -> Path:
    DIST.mkdir(exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in SHIP_FILES:
            archive.write(ROOT / rel, PREFIX + rel)
        for tree in SHIP_TREES:
            for path in _tree_files(tree):
                archive.write(path, PREFIX + path.relative_to(ROOT).as_posix())
    return ZIP_PATH


@pytest.fixture(scope="module")
def bundle():
    build_zip()
    with zipfile.ZipFile(ZIP_PATH) as archive:
        yield archive


def _expanded_entries(bundle):
    """Inspect compressed document contents as well as plain source files."""
    for name in bundle.namelist():
        data = bundle.read(name)
        if name.endswith(".docx"):
            with zipfile.ZipFile(io.BytesIO(data)) as docx:
                for member in docx.namelist():
                    yield name + "!" + member, docx.read(member)
        else:
            yield name, data


def test_zip_opens_cleanly(bundle):
    assert bundle.testzip() is None
    assert len(bundle.namelist()) == len(set(bundle.namelist()))


def test_public_entrypoints_present(bundle):
    for name in SHIP_FILES + ["prototype/index.html", ".github/workflows/test.yml"]:
        assert PREFIX + name in bundle.namelist()


def test_no_local_or_working_material(bundle):
    forbidden_dirs = {".git", ".venv", "venv", "dist", "docs", "notes", "sdd"}
    forbidden_names = {".env", "AGENTS.md", "CLAUDE.md", "report.pdf"}
    for name in bundle.namelist():
        parts = Path(name).parts[1:]
        assert not forbidden_dirs.intersection(parts), name
        assert not forbidden_names.intersection(parts), name
        assert not any(part.startswith("~$") for part in parts), name


def test_only_allowlisted_paths_ship(bundle):
    for name in bundle.namelist():
        assert name.startswith(PREFIX)
        rel = name[len(PREFIX):]
        assert rel in SHIP_FILES or rel.split("/")[0] in SHIP_TREES, name


def test_env_example_has_no_credential(bundle):
    body = bundle.read(PREFIX + ".env.example").decode()
    assert "OPENAI_API_KEY=" in body
    assert re.search(r"^OPENAI_API_KEY=\S", body, re.MULTILINE) is None


def test_no_secret_patterns_including_document_xml(bundle):
    patterns = [
        rb"sk-[A-Za-z0-9_-]{20,}",
        rb"gh[pousr]_[A-Za-z0-9]{30,}",
        rb"github_pat_[A-Za-z0-9_]{30,}",
        rb"AKIA[A-Z0-9]{16}",
        rb"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    ]
    for name, data in _expanded_entries(bundle):
        for pattern in patterns:
            assert not re.search(pattern, data), f"secret-shaped value in {name}"


def test_all_contracts_are_synthetic(bundle):
    contracts = json.loads(bundle.read(PREFIX + "data/contracts.json"))
    assert len(contracts) == 11
    assert all(doc["source"] == "synthetic" for doc in contracts)


def test_contract_hashes_match_published_text(bundle):
    contracts = json.loads(bundle.read(PREFIX + "data/contracts.json"))
    for doc in contracts:
        digest = hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()
        assert doc["file_hash"] == digest, doc["document_id"]


def test_redline_contains_real_tracked_changes(bundle):
    data = bundle.read(PREFIX + "redline/nda_003_redline.docx")
    with zipfile.ZipFile(io.BytesIO(data)) as docx:
        assert docx.testzip() is None
        for name in docx.namelist():
            ElementTree.fromstring(docx.read(name))
        document = ElementTree.fromstring(docx.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        assert document.findall(".//w:del", ns)
        assert document.findall(".//w:ins", ns)


def test_browser_assets_and_data_are_self_contained(bundle):
    html = bundle.read(PREFIX + "prototype/index.html").decode()
    for src in re.findall(r'(?:src|href)="([^"]+)"', html):
        if src.startswith("#") or "?" in src:
            continue
        assert not re.match(r"(?:https?:)?//", src), src
        assert PREFIX + "prototype/" + src in bundle.namelist(), src


def test_evaluation_rows_cover_all_contracts(bundle):
    contracts = json.loads(bundle.read(PREFIX + "data/contracts.json"))
    rows = json.loads(bundle.read(PREFIX + "evaluation/results.json"))
    assert {row["document_id"] for row in rows} == {
        doc["document_id"] for doc in contracts
    }


def test_core_pipeline_and_tests_ship(bundle):
    names = set(bundle.namelist())
    for path in (ROOT / "pipeline").glob("*.py"):
        assert PREFIX + path.relative_to(ROOT).as_posix() in names
    for path in (ROOT / "tests").glob("test_*.py"):
        assert PREFIX + path.relative_to(ROOT).as_posix() in names


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info("Built %s", build_zip())
