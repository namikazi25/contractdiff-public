"""Tests for the LLM client and configuration."""
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import llm_client  # noqa: E402


def test_missing_env_produces_clear_message(tmp_path):
    with pytest.raises(llm_client.MissingConfigError) as excinfo:
        llm_client.load_config(env_file=tmp_path / ".env", environ={})
    message = str(excinfo.value)
    for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"):
        assert name in message
    assert ".env.example" in message
    assert "precomputed outputs" in message


def test_config_loads_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "OPENAI_BASE_URL=https://example.test/api/v1/\n"
        "OPENAI_API_KEY=test-key\n"
        "OPENAI_MODEL=test-model\n"
        "OPENAI_TEMPERATURE=0.2\n"
        "OPENAI_TIMEOUT_SECONDS=30\n",
        encoding="utf-8",
    )
    config = llm_client.load_config(env_file=env_file, environ={})
    assert config.base_url == "https://example.test/api/v1"
    assert config.api_key == "test-key"
    assert config.model == "test-model"
    assert config.temperature == 0.2
    assert config.timeout_seconds == 30


def test_environment_overrides_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_MODEL=file-model\n", encoding="utf-8")
    config = llm_client.load_config(
        env_file=env_file,
        environ={
            "OPENAI_BASE_URL": "https://example.test",
            "OPENAI_API_KEY": "k",
            "OPENAI_MODEL": "env-model",
        },
    )
    assert config.model == "env-model"


def test_defaults_when_optional_vars_absent():
    config = llm_client.load_config(
        env_file=Path("/nonexistent/.env"),
        environ={
            "OPENAI_BASE_URL": "https://example.test",
            "OPENAI_API_KEY": "k",
            "OPENAI_MODEL": "m",
        },
    )
    assert config.temperature == 0
    assert config.timeout_seconds == 60


def test_non_numeric_optional_var_fails_clearly():
    with pytest.raises(llm_client.ConfigError, match="must be numeric"):
        llm_client.load_config(
            env_file=Path("/nonexistent/.env"),
            environ={
                "OPENAI_BASE_URL": "https://example.test",
                "OPENAI_API_KEY": "k",
                "OPENAI_MODEL": "m",
                "OPENAI_TEMPERATURE": "warm",
            },
        )


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_chat_completion_records_audit_data(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["auth"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return FakeResponse({
            "model": "returned-model-id",
            "choices": [{"message": {"content": '{"findings": []}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    config = llm_client.LLMConfig(
        base_url="https://example.test/api/v1",
        api_key="secret-key",
        model="configured-model",
        temperature=0,
        timeout_seconds=42,
    )
    result = llm_client.chat_completion(
        [{"role": "user", "content": "ping"}], config=config
    )

    assert captured["url"] == "https://example.test/api/v1/chat/completions"
    assert captured["payload"]["model"] == "configured-model"
    assert captured["payload"]["temperature"] == 0
    assert captured["auth"] == "Bearer secret-key"
    assert captured["timeout"] == 42

    assert result.content == '{"findings": []}'
    assert result.model == "returned-model-id"
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert result.cost_usd is None  # never invented
    assert result.wall_time_ms >= 0


def test_chat_completion_records_cost_when_returned(monkeypatch):
    def fake_urlopen(request, timeout):
        return FakeResponse({
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"cost": 0.0012},
        })

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    config = llm_client.LLMConfig("https://example.test", "k", "m", 0, 60)
    result = llm_client.chat_completion([{"role": "user", "content": "ping"}], config)
    assert result.cost_usd == 0.0012


def test_bad_response_shape_raises_llm_error(monkeypatch):
    def fake_urlopen(request, timeout):
        return FakeResponse({"unexpected": True})

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    config = llm_client.LLMConfig("https://example.test", "k", "m", 0, 60)
    with pytest.raises(llm_client.LLMError, match="choices"):
        llm_client.chat_completion([{"role": "user", "content": "ping"}], config)


def test_read_timeout_raises_llm_error(monkeypatch):
    import socket

    def fake_urlopen(request, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    config = llm_client.LLMConfig("https://example.test", "k", "m", 0, 60)
    with pytest.raises(llm_client.LLMError, match="endpoint call failed"):
        llm_client.chat_completion([{"role": "user", "content": "ping"}], config)


def test_http_error_raises_llm_error_with_status_and_detail(monkeypatch):
    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error": "invalid api key"}'),
        )

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    config = llm_client.LLMConfig("https://example.test", "k", "m", 0, 60)
    with pytest.raises(llm_client.LLMError) as excinfo:
        llm_client.chat_completion([{"role": "user", "content": "ping"}], config)
    assert "HTTP 401" in str(excinfo.value)
    assert "invalid api key" in str(excinfo.value)
