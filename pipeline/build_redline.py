#!/usr/bin/env python3
"""Build the tracked-changes redline DOCX for NDA-003.

Reproduces NDA-003 as a Word document with one genuine tracked change in
clause 8 (Limitation of liability): the deviant unlimited-liability body is
marked deleted (``w:del``) and the approved template cap language is marked
inserted (``w:ins``). Everything is derived from the frozen canonical data —
``contracts.json``, ``templates.json``, ``playbook.json``, and
``routing.json`` — so the artefact is regenerable and never hand-edited.
Stdlib only; reruns are byte-identical (fixed change date and zip metadata).
"""
from __future__ import annotations

import argparse
import json
import logging
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEFAULT_OUTPUT = ROOT / "redline" / "nda_003_redline.docx"

DOCUMENT_ID = "NDA-003"
DEVIANT_PREFIX = "Each party accepts unlimited liability"
TEMPLATE_CLAUSE_ID = "TPL-MNDA-LIABILITY-001"
RULE_ID = "MNDA-LIABILITY-001"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CHANGE_AUTHOR = "ContractDiff pipeline (example output)"
CHANGE_DATE = "2026-08-26T09:00:00Z"
ZIP_DATE = (2026, 8, 26, 9, 0, 0)

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>
"""

CORE_PROPS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>NDA-003 redline — restore the mutual twelve-month liability cap</dc:title><dc:creator>ContractDiff pipeline (example output)</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">2026-08-26T09:00:00Z</dcterms:created></cp:coreProperties>
"""


def load_inputs():
    contracts = json.loads((DATA_DIR / "contracts.json").read_text(encoding="utf-8"))
    templates = json.loads((DATA_DIR / "templates.json").read_text(encoding="utf-8"))
    playbooks = json.loads((DATA_DIR / "playbook.json").read_text(encoding="utf-8"))
    routing = json.loads((DATA_DIR / "routing.json").read_text(encoding="utf-8"))

    contract = next(c for c in contracts if c["document_id"] == DOCUMENT_ID)
    template_clause = next(
        clause
        for template in templates
        for clause in template["clauses"]
        if clause["template_clause_id"] == TEMPLATE_CLAUSE_ID
    )
    rule = next(
        rule
        for playbook in playbooks
        for rule in playbook["rules"]
        if rule["rule_id"] == RULE_ID
    )
    record = next(r for r in routing if r["document_id"] == DOCUMENT_ID)
    return contract, template_clause, rule, record


def split_deviant_clause(contract):
    """Return (paragraphs, index) of the clause-8 paragraph to be redlined."""
    paragraphs = contract["text"].split("\n\n")
    for index, paragraph in enumerate(paragraphs):
        if DEVIANT_PREFIX in paragraph:
            return paragraphs, index
    raise SystemExit(
        f"{DOCUMENT_ID}: deviant liability clause not found "
        f"(expected a paragraph containing {DEVIANT_PREFIX!r}); the frozen "
        "dataset must have changed — regenerate deliberately, not by hand."
    )


def runs(text):
    """Runs for text, rendering embedded newlines as line breaks."""
    parts = [
        f'<w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r>'
        for line in text.split("\n")
    ]
    return "<w:r><w:br/></w:r>".join(parts)


def paragraph(text):
    return f"<w:p>{runs(text)}</w:p>"


def tracked_change_paragraph(prefix, deleted, inserted):
    deletion = (
        f'<w:del w:id="1" w:author="{escape(CHANGE_AUTHOR)}" w:date="{CHANGE_DATE}">'
        f'<w:r><w:delText xml:space="preserve">{escape(deleted)}</w:delText></w:r></w:del>'
    )
    insertion = (
        f'<w:ins w:id="2" w:author="{escape(CHANGE_AUTHOR)}" w:date="{CHANGE_DATE}">'
        f'<w:r><w:t xml:space="preserve">{escape(inserted)}</w:t></w:r></w:ins>'
    )
    return f"<w:p>{runs(prefix)}{deletion}{insertion}</w:p>"


def build_document_xml(contract, template_clause, rule, record):
    paragraphs, deviant_index = split_deviant_clause(contract)
    header = [
        paragraph(
            "ContractDiff tracked-changes redline — example output produced by "
            "the pipeline."
        ),
        paragraph(
            f"Document: {DOCUMENT_ID} — {contract['counterparty']} "
            f"(record {contract['contract_record_id']}, "
            f"version {contract['version']})."
        ),
        paragraph(
            f"Finding: {rule['candidate_issue']} — rule {RULE_ID}, "
            f"severity {rule['severity']}, route {record['final_route']} "
            "(data/routing.json)."
        ),
        paragraph(
            f"Playbook preferred position: {rule['preferred_position']}. "
            f"Suggested action: {rule['suggested_fallback_language']}"
        ),
        paragraph(""),
    ]
    body = []
    for index, text in enumerate(paragraphs):
        if index == deviant_index:
            prefix, deviant = text.split(DEVIANT_PREFIX, 1)
            body.append(
                tracked_change_paragraph(
                    prefix, DEVIANT_PREFIX + deviant, template_clause["text"]
                )
            )
        else:
            body.append(paragraph(text))
    sect_pr = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        + "".join(header + body)
        + sect_pr
        + "</w:body></w:document>\n"
    )


def write_docx(path, document_xml):
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = (
        ("[Content_Types].xml", CONTENT_TYPES),
        ("_rels/.rels", RELS),
        ("docProps/core.xml", CORE_PROPS),
        ("word/document.xml", document_xml),
    )
    with zipfile.ZipFile(path, "w") as bundle:
        for name, payload in parts:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    contract, template_clause, rule, record = load_inputs()
    document_xml = build_document_xml(contract, template_clause, rule, record)
    write_docx(args.output, document_xml)
    logger.info("wrote %s (tracked-changes redline for %s)", args.output, DOCUMENT_ID)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
