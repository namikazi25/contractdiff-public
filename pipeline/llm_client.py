"""OpenAI-compatible chat completions client.

Works against any endpoint implementing the chat completions shape. Nothing
here is specific to one provider: no provider-specific structured output
features are used — JSON is asked for in the prompt and validated locally.

Configuration comes from environment variables, optionally via a local
``.env`` file (never committed). Real environment variables
take precedence over the file.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

REQUIRED_VARS = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")

MISSING_CONFIG_MESSAGE = (
    "LLM configuration is missing: {missing}.\n"
    "Copy .env.example to .env at the repository root and fill in your key, "
    "or set the variables in your environment.\n"
    "The demo does not need a key: it replays the precomputed outputs in "
    "data/model_outputs.json."
)


class ConfigError(RuntimeError):
    """Raised when required LLM configuration is missing or invalid."""


class MissingConfigError(ConfigError):
    """Raised when live generation credentials or endpoint settings are absent."""


class LLMError(RuntimeError):
    """Raised when the endpoint call fails or returns an unusable shape."""


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float
    timeout_seconds: float


@dataclass
class LLMResult:
    content: str
    model: str | None
    wall_time_ms: int
    usage: dict | None
    cost_usd: float | None


def _parse_env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_config(env_file: Path = ENV_FILE, environ: dict | None = None) -> LLMConfig:
    """Load configuration, raising ConfigError with a clear message if incomplete."""
    values = {}
    if env_file.is_file():
        values.update(_parse_env_file(env_file))
    values.update(os.environ if environ is None else environ)

    missing = [name for name in REQUIRED_VARS if not values.get(name)]
    if missing:
        raise MissingConfigError(
            MISSING_CONFIG_MESSAGE.format(missing=", ".join(missing))
        )

    try:
        temperature = float(values.get("OPENAI_TEMPERATURE", "0") or 0)
        timeout = float(values.get("OPENAI_TIMEOUT_SECONDS", "60") or 60)
    except ValueError as exc:
        raise ConfigError(
            f"OPENAI_TEMPERATURE and OPENAI_TIMEOUT_SECONDS must be numeric: {exc}"
        ) from exc

    return LLMConfig(
        base_url=values["OPENAI_BASE_URL"].rstrip("/"),
        api_key=values["OPENAI_API_KEY"],
        model=values["OPENAI_MODEL"],
        temperature=temperature,
        timeout_seconds=timeout,
    )


def chat_completion(messages: list, config: LLMConfig | None = None) -> LLMResult:
    """Call ``{base_url}/chat/completions`` and return the content plus audit data.

    Records wall time, token usage if returned, cost if returned (null
    otherwise — cost is never invented), and the returned model ID.
    """
    if config is None:
        config = load_config()

    payload = json.dumps({
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMError(f"endpoint returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        # socket.timeout is only an alias of TimeoutError from Python 3.10;
        # on the repo's Python 3.9 a read timeout escapes without it.
        raise LLMError(f"endpoint call failed: {exc}") from exc
    wall_time_ms = round((time.monotonic() - started) * 1000)
    logger.debug("chat completion returned in %dms", wall_time_ms)

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            "response did not contain choices[0].message.content: "
            f"{body!r:.500}"
        ) from exc

    usage = body.get("usage") or None
    # Known limitation: OpenRouter only returns usage.cost when the request
    # carries {"usage": {"include": true}}; without it cost stays null,
    # which is honest but leaves the evaluation cost column empty.
    cost = body.get("cost")
    if cost is None and isinstance(usage, dict):
        cost = usage.get("cost")

    return LLMResult(
        content=content,
        model=body.get("model"),
        wall_time_ms=wall_time_ms,
        usage=usage,
        cost_usd=cost,
    )
