"""
Live model connectivity tests.

These tests make real API calls to verify that each provider's backend can
successfully complete a minimal chat request.  They are gated behind the
``live`` pytest marker so they won't run in normal CI:

    pytest tests/test_live_models.py -v -m live --tb=short

Requirements:
  - GitHub Copilot: valid GitHub OAuth token in ~/.config/litellm/github_copilot/access-token
  - Groq: GROQ_API_KEY set in .env or environment
  - Gemini: valid Gemini CLI OAuth credentials
  - Codex: valid Codex session in ~/.config/litellm/codex/
"""
from __future__ import annotations

import pytest

from chat2api.models.openai_types import ChatCompletionRequest, ChatMessage
from chat2api.models.tiers import ModelTarget

# A tiny, deterministic prompt that every model can answer quickly.
_HELLO_REQUEST = ChatCompletionRequest(
    model="test",
    messages=[ChatMessage(role="user", content="Say hello in exactly two words.")],
    stream=False,
    temperature=0,
    max_tokens=20,
)


def _generate(backend, provider: str, model_id: str) -> str:
    """Call backend.generate_text and return the result."""
    target = ModelTarget(
        requested_name=model_id,
        provider=provider,
        model_id=model_id,
        quota_group=f"{provider}-test",
        fallback_provider=provider,
        fallback_model_id=model_id,
    )
    return backend.generate_text(target, _HELLO_REQUEST)


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_copilot_gpt4o():
    """Verify GitHub Copilot can serve gpt-4o (included model)."""
    from chat2api.anti_detection.tls_client import TLSClient
    from chat2api.providers.copilot import CopilotBackend

    tls = TLSClient(strategy="native")
    backend = CopilotBackend(tls)
    result = _generate(backend, "copilot", "gpt-4o")
    assert len(result.strip()) > 0, f"Empty response from copilot/gpt-4o: {result!r}"


@pytest.mark.live
def test_live_copilot_claude_sonnet():
    """Verify GitHub Copilot can serve claude-sonnet-4.5 (premium model)."""
    from chat2api.anti_detection.tls_client import TLSClient
    from chat2api.providers.copilot import CopilotBackend

    tls = TLSClient(strategy="native")
    backend = CopilotBackend(tls)
    result = _generate(backend, "copilot", "claude-sonnet-4.5")
    assert len(result.strip()) > 0, f"Empty response from copilot/claude-sonnet-4.5: {result!r}"


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_groq_llama():
    """Verify Groq can serve llama-3.3-70b-versatile."""
    from chat2api.anti_detection.tls_client import TLSClient
    from chat2api.providers.openai_compat import OpenAICompatBackend

    tls = TLSClient(strategy="native")
    backend = OpenAICompatBackend("groq", "https://api.groq.com/openai/v1", tls)
    result = _generate(backend, "groq", "llama-3.3-70b-versatile")
    assert len(result.strip()) > 0, f"Empty response from groq/llama: {result!r}"


@pytest.mark.live
def test_live_groq_llama_small():
    """Verify Groq can serve llama-3.1-8b-instant."""
    from chat2api.anti_detection.tls_client import TLSClient
    from chat2api.providers.openai_compat import OpenAICompatBackend

    tls = TLSClient(strategy="native")
    backend = OpenAICompatBackend("groq", "https://api.groq.com/openai/v1", tls)
    result = _generate(backend, "groq", "llama-3.1-8b-instant")
    assert len(result.strip()) > 0, f"Empty response from groq/llama-small: {result!r}"


@pytest.mark.live
def test_live_groq_llama4():
    """Verify Groq can serve meta-llama/llama-4-scout-17b-16e-instruct."""
    from chat2api.anti_detection.tls_client import TLSClient
    from chat2api.providers.openai_compat import OpenAICompatBackend

    tls = TLSClient(strategy="native")
    backend = OpenAICompatBackend("groq", "https://api.groq.com/openai/v1", tls)
    result = _generate(backend, "groq", "meta-llama/llama-4-scout-17b-16e-instruct")
    assert len(result.strip()) > 0, f"Empty response from groq/llama-4-scout: {result!r}"


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_gemini():
    """Verify Gemini backend can serve a model."""
    from chat2api.anti_detection.tls_client import TLSClient
    from chat2api.providers.gemini import GeminiBackend

    tls = TLSClient(strategy="native")
    backend = GeminiBackend(tls)
    result = _generate(backend, "gemini", "gemini-2.5-flash")
    assert len(result.strip()) > 0, f"Empty response from gemini: {result!r}"


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_codex():
    """Verify Codex backend can serve a model."""
    from chat2api.anti_detection.tls_client import TLSClient
    from chat2api.providers.codex import CodexBackend

    tls = TLSClient(strategy="native")
    backend = CodexBackend(tls)
    result = _generate(backend, "codex", "gpt-5.4-mini")
    assert len(result.strip()) > 0, f"Empty response from codex: {result!r}"
