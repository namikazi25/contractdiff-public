"""Redline artefact tests."""
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import build_redline  # noqa: E402

DOCX_PATH = ROOT / "redline" / "nda_003_redline.docx"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def document_xml(path=DOCX_PATH):
    with zipfile.ZipFile(path) as bundle:
        return bundle.read("word/document.xml").decode("utf-8")


def nda_003_clause_8_body():
    contract = next(
        doc
        for doc in json.loads((ROOT / "data" / "contracts.json").read_text(encoding="utf-8"))
        if doc["document_id"] == "NDA-003"
    )
    paragraph = next(
        part
        for part in contract["text"].split("\n\n")
        if "8. Limitation of liability" in part
    )
    return paragraph.split("\n", 1)[1]


def test_docx_unzips_and_document_parses_as_xml():
    with zipfile.ZipFile(DOCX_PATH) as bundle:
        assert bundle.testzip() is None
        assert "word/document.xml" in bundle.namelist()
    root = ET.fromstring(document_xml())
    assert root.tag == f"{W}document"


def test_contains_genuine_tracked_insertion_and_deletion():
    root = ET.fromstring(document_xml())
    insertions = root.findall(f".//{W}ins")
    deletions = root.findall(f".//{W}del")
    assert len(insertions) >= 1
    assert len(deletions) >= 1
    for change in insertions + deletions:
        assert change.get(f"{W}author") == "ContractDiff pipeline (example output)"
        assert change.get(f"{W}date")


def test_inserted_text_restores_the_template_liability_cap():
    root = ET.fromstring(document_xml())
    inserted = "".join(
        node.text or ""
        for node in root.findall(f".//{W}ins/{W}r/{W}t")
    )
    assert "twelve months" in inserted
    assert "capped" in inserted
    templates = json.loads((ROOT / "data" / "templates.json").read_text(encoding="utf-8"))
    template_clause = next(
        clause
        for template in templates
        for clause in template["clauses"]
        if clause["template_clause_id"] == "TPL-MNDA-LIABILITY-001"
    )
    assert inserted == template_clause["text"]


def test_deleted_text_is_the_frozen_nda_003_clause_verbatim():
    root = ET.fromstring(document_xml())
    deleted = "".join(
        node.text or ""
        for node in root.findall(f".//{W}del/{W}r/{W}delText")
    )
    assert deleted == nda_003_clause_8_body()


def test_labelled_as_example_pipeline_output():
    xml = document_xml()
    assert "example output produced by the pipeline" in xml
    with zipfile.ZipFile(DOCX_PATH) as bundle:
        core = bundle.read("docProps/core.xml").decode("utf-8")
    assert "ContractDiff pipeline (example output)" in core


def test_rebuild_is_byte_identical(tmp_path):
    first = tmp_path / "first.docx"
    second = tmp_path / "second.docx"
    assert build_redline.main(["--output", str(first)]) == 0
    assert build_redline.main(["--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == DOCX_PATH.read_bytes()
