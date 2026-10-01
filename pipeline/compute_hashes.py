#!/usr/bin/env python3
"""Replace every PENDING-HASH file_hash with the SHA-256 of the text.

Idempotent: an already-computed hash is recomputed and must match, so the
script doubles as a tamper check on the frozen data.
"""
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_FILE = ROOT / "data" / "contracts.json"


def main() -> None:
    documents = json.loads(CONTRACTS_FILE.read_text(encoding="utf-8"))

    for doc in documents:
        digest = hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()
        existing = doc["file_hash"]
        if existing not in ("PENDING-HASH", digest):
            raise SystemExit(
                f"{doc['document_id']}: stored hash {existing} does not match "
                f"text hash {digest}; the frozen data may have been edited"
            )
        doc["file_hash"] = digest

    CONTRACTS_FILE.write_text(
        json.dumps(documents, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("hashed %d documents in %s", len(documents), CONTRACTS_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
