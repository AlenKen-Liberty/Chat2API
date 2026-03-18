from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from chat2api.account.gemini_account import GeminiAuthError, ensure_fresh_account, list_accounts
from chat2api.anti_detection.tls_client import TLSClient, TLSClientDependencyError
from chat2api.models.openai_types import ChatCompletionRequest, content_to_text, split_system_messages
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

# Gemini CLI internal endpoint — project_id goes in the request body, NOT the URL
STREAM_URL = "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"


class GeminiBackend(ProviderBackend):
    provider_name = "gemini"

    def __init__(self, tls_client: TLSClient):
        self.tls_client = tls_client

    def _get_accounts(self):
        """Return all enabled Gemini accounts sorted by least-recently-used first."""
        accounts = [acc for acc in list_accounts() if not acc.disabled]
        if not accounts:
            raise ProviderAuthError("No enabled Gemini accounts found")
        return sorted(accounts, key=lambda a: a.last_used)

    def stream_text(self, target: ModelTarget, request: ChatCompletionRequest) -> Iterator[str]:
        """
        Try each Gemini account in LRU order.
        Transparently skip accounts that return 429; only raise ProviderRateLimitError
        when ALL accounts are exhausted so the router can do cross-provider fallback.
        """
        try:
            accounts = self._get_accounts()
        except ProviderAuthError:
            raise

        last_err: Exception | None = None
        for account in accounts:
            try:
                account = ensure_fresh_account(account)
            except GeminiAuthError as exc:
                logger.warning("Gemini account %s token refresh failed: %s", account.email, exc)
                last_err = exc
                continue

            if not account.project_id:
                logger.warning("Gemini account %s missing project_id, skipping", account.email)
                continue

            try:
                yield from self._stream_from_account(account, target, request)
                return  # success — stop iterating accounts
            except ProviderRateLimitError as exc:
                logger.info("Gemini account %s rate-limited, trying next account…", account.email)
                last_err = exc
                continue

        # All accounts exhausted
        raise ProviderRateLimitError(
            f"All {len(accounts)} Gemini account(s) are rate-limited or unavailable. "
            f"Last error: {last_err}"
        )

    def _stream_from_account(self, account, target: ModelTarget, request: ChatCompletionRequest) -> Iterator[str]:
        payload = self._build_payload(target, request, account.project_id)
        headers = {
            "Authorization": f"Bearer {account.token.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "GeminiCLI/1.0.0",
            "x-goog-api-client": "GeminiCLI/1.0.0",
            "Accept": "text/event-stream",
        }

        try:
            with self.tls_client.stream("POST", STREAM_URL, headers=headers, json=payload) as response:
                if response.status_code == 401:
                    raise ProviderAuthError(f"Gemini authentication failed for {account.email}")
                if response.status_code == 429:
                    raise ProviderRateLimitError(f"Gemini rate limit reached for {account.email}")
                if response.status_code >= 400:
                    raise ProviderRequestError(self._response_error(response))

                previous_text = ""
                for event in iter_sse_json(response.iter_lines()):
                    if "error" in event:
                        raise ProviderRequestError(json.dumps(event["error"], ensure_ascii=False))
                    current_text = self._extract_text(event)
                    if not current_text:
                        continue
                    if current_text.startswith(previous_text):
                        delta = current_text[len(previous_text):]
                    else:
                        delta = current_text
                    previous_text = current_text
                    if delta:
                        yield delta
        except TLSClientDependencyError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    def _build_payload(
        self, target: ModelTarget, request: ChatCompletionRequest, project_id: str | None
    ) -> dict[str, Any]:
        system_text, messages = split_system_messages(request.messages)
        if not messages:
            raise ProviderRequestError("At least one non-system message is required")

        contents = [
            {
                "role": self._map_role(message.role),
                "parts": [{"text": content_to_text(message.content)}],
            }
            for message in messages
            if content_to_text(message.content)
        ]
        if not contents:
            raise ProviderRequestError("No text content found in messages")

        inner: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature if request.temperature is not None else 1.0,
                "topP": request.top_p if request.top_p is not None else 0.95,
                "topK": 64,
                "maxOutputTokens": request.max_tokens or 65536,
                "responseMimeType": "text/plain",
            },
            "session_id": str(uuid.uuid4()),
        }
        if system_text:
            inner["systemInstruction"] = {"parts": [{"text": system_text}]}

        # Gemini CLI internal API: model and project are top-level; contents go inside "request"
        payload: dict[str, Any] = {
            "model": target.model_id,          # NO "models/" prefix
            "project": project_id or "",
            "user_prompt_id": str(uuid.uuid4()),
            "request": inner,
        }
        return payload

    @staticmethod
    def _map_role(role: str) -> str:
        if role not in {"user", "assistant"}:
            return "user"
        return role

    @staticmethod
    def _extract_text(event: dict[str, Any]) -> str:
        chunks: list[str] = []
        # Gemini CLI internal SSE: {"response": {"candidates": [...]}}
        inner = event.get("response") or event  # fall back to top-level for safety
        for candidate in inner.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                text = part.get("text")
                if text:
                    chunks.append(text)
        return "".join(chunks)

    @staticmethod
    def _response_error(response: Any) -> str:
        try:
            payload = response.text
        except Exception:
            payload = "<no response body>"
        return f"Gemini request failed: HTTP {response.status_code} {payload}"
