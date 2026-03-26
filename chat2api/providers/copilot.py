"""
GitHub Copilot provider backend.

Uses the standard OpenAI /chat/completions format against
api.individual.githubcopilot.com (or whichever endpoint the plan dictates).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from chat2api.account.copilot_account import CopilotAuthError, get_copilot_account
from chat2api.anti_detection.tls_client import TLSClient, TLSClientDependencyError
from chat2api.models.openai_types import ChatCompletionRequest, content_to_text
from chat2api.models.tiers import ModelTarget
from chat2api.protocol.sse import iter_sse_json
from chat2api.providers.base import (
    ProviderAuthError,
    ProviderBackend,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)


class CopilotBackend(ProviderBackend):
    provider_name = "copilot"

    def __init__(self, tls_client: TLSClient):
        self.tls_client = tls_client

    def stream_text(self, target: ModelTarget, request: ChatCompletionRequest) -> Iterator[str]:
        try:
            account = get_copilot_account()
            session_token = account.session_token
            api_base = account.api_base
        except CopilotAuthError as exc:
            raise ProviderAuthError(str(exc)) from exc

        url = f"{api_base}/chat/completions"
        payload = self._build_payload(target, request)
        headers = {
            "Authorization": f"Bearer {session_token}",
            "Content-Type": "application/json",
            "Editor-Version": "vscode/1.96.0",
            "Editor-Plugin-Version": "copilot-chat/0.24.0",
            "Copilot-Integration-Id": "vscode-chat",
            "User-Agent": "GitHubCopilotChat/0.24.0",
            "Accept": "text/event-stream",
        }

        try:
            with self.tls_client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code == 401:
                    raise ProviderAuthError("Copilot authentication failed")
                if response.status_code == 429:
                    raise ProviderRateLimitError("Copilot rate limit reached")
                if response.status_code >= 400:
                    raise ProviderRequestError(self._response_error(response))

                for event in iter_sse_json(response.iter_lines()):
                    # Standard OpenAI streaming format
                    choices = event.get("choices") or []
                    for choice in choices:
                        delta = choice.get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
        except TLSClientDependencyError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    @staticmethod
    def _build_payload(target: ModelTarget, request: ChatCompletionRequest) -> dict[str, Any]:
        messages = [
            {"role": m.role, "content": content_to_text(m.content)}
            for m in request.messages
            if content_to_text(m.content)
        ]
        if not messages:
            raise ProviderRequestError("At least one message with text content is required")

        payload: dict[str, Any] = {
            "model": target.model_id,
            "messages": messages,
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        return payload

    @staticmethod
    def _response_error(response: Any) -> str:
        try:
            for line in response.iter_lines():
                line_str = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                if line_str.strip():
                    return f"Copilot request failed: HTTP {response.status_code} {line_str}"
        except Exception:
            pass
        return f"Copilot request failed: HTTP {response.status_code}"
