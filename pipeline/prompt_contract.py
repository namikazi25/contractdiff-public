"""Prompt contract for the AI review path.

Loads and renders the versioned prompt templates from ``prompts/``. Contract
text is always inserted as untrusted data between explicit begin/end
delimiters; the templates never give the model authority over severity,
route, or approval.

The prompt version string ``contract-review.v1`` is recorded in the audit
metadata of every generated output.
"""
from pathlib import Path
from string import Template

PROMPT_VERSION = "contract-review.v1"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

SYSTEM_PROMPT_FILE = "contract_review_v1.txt"
USER_PROMPT_FILE = "contract_review_user_v1.txt"
RETRY_PROMPT_FILE = "contract_review_retry_v1.txt"

BEGIN_DELIMITER = "<contract>"
END_DELIMITER = "</contract>"


def _load(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def render_system_prompt() -> str:
    """Return the system prompt, which marks contract text as untrusted."""
    return _load(SYSTEM_PROMPT_FILE)


def render_user_prompt(
    *,
    metadata: str,
    template: str,
    playbook_rules: str,
    contract_text: str,
    schema_summary: str,
) -> str:
    """Render the user prompt with the contract text inside the delimiters.

    All arguments are pre-serialised strings (callers JSON-encode structured
    values). Substitution is one-pass, so braces or marker-like text inside
    ``contract_text`` cannot affect the template.

    Raises ValueError if the contract text contains the end delimiter: a
    crafted document must not be able to "close" the delimiter and demote
    its own tail from data to instruction-position text. The pipeline
    catches this and routes the document to manual review.
    """
    if END_DELIMITER in contract_text:
        raise ValueError(
            "contract text contains the end delimiter "
            f"'{END_DELIMITER}'; refusing to render — route this document "
            "to manual review"
        )
    return Template(_load(USER_PROMPT_FILE)).substitute(
        metadata=metadata,
        template=template,
        playbook_rules=playbook_rules,
        contract_text=contract_text,
        schema_summary=schema_summary,
    )


def render_retry_prompt(errors: str) -> str:
    """Render the retry prompt with the validation errors fed back."""
    return Template(_load(RETRY_PROMPT_FILE)).substitute(errors=errors)
