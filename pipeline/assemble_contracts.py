#!/usr/bin/env python3
"""Assemble data/contracts.json from authored fragments.

Reads one JSON object per fragment from data/fragments/, orders the
documents canonically, derives NDA-001-DUP from NDA-001 (byte-identical
text; only document_id, contract_record_id, and received_at differ), and
writes data/contracts.json. Every file_hash stays "PENDING-HASH" until
compute_hashes.py runs.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS_DIR = ROOT / "data" / "fragments"
OUT_FILE = ROOT / "data" / "contracts.json"

CANONICAL_ORDER = [
    "NDA-001",
    "NDA-002",
    "NDA-003",
    "NDA-004",
    "NDA-005",
    "NDA-006",
    "NDA-007",
    "NDA-008",
    "ORDER-001",
    "UNKNOWN-001",
]

# NDA-001-DUP is a byte-identical resubmission of NDA-001.
DUPLICATE = {
    "document_id": "NDA-001-DUP",
    "source_document_id": "NDA-001",
    "contract_record_id": "CR-ACME-NDA-DUP",
    "received_at": "2026-08-29T13:25:00Z",
}


def main() -> None:
    fragments = {}
    for path in sorted(FRAGMENTS_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc_id = doc["document_id"]
        if doc_id in fragments:
            raise SystemExit(f"duplicate fragment for {doc_id}")
        fragments[doc_id] = doc

    missing = [doc_id for doc_id in CANONICAL_ORDER if doc_id not in fragments]
    if missing:
        raise SystemExit(f"missing fragments: {', '.join(missing)}")

    documents = [fragments[doc_id] for doc_id in CANONICAL_ORDER]

    source = fragments[DUPLICATE["source_document_id"]]
    duplicate = dict(source)
    duplicate["document_id"] = DUPLICATE["document_id"]
    duplicate["contract_record_id"] = DUPLICATE["contract_record_id"]
    duplicate["received_at"] = DUPLICATE["received_at"]
    documents.append(duplicate)

    OUT_FILE.write_text(
        json.dumps(documents, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %s (%d documents)", OUT_FILE, len(documents))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
