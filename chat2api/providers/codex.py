from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from chat2api.account.codex_account import CodexAuthError, ensure_fresh_account, list_accounts
from chat2api.anti_detection.tls_client import TLSClient, TLSClientDependencyError
from chat2api.models.openai_types import ChatCompletionRequest, ChatMessage, content_to_text, split_system_messages
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

# ChatGPT backend endpoint used by Codex CLI with auth_mode: chatgpt
# (tokens from auth.openai.com have only api.connectors.* scopes, so /v1/responses → 401)
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


class CodexBackend(ProviderBackend):
    provider_name = "codex"

    def __init__(self, tls_client: TLSClient):
        self.tls_client = tls_client

    def _get_accounts(self):
        """Return all enabled Codex accounts sorted by least-recently-used first."""
        accounts = [acc for acc in list_accounts() if not acc.disabled]
        if not accounts:
            raise ProviderAuthError("No enabled Codex accounts found")
        return sorted(accounts, key=lambda a: a.last_used)

    def stream_text(self, target: ModelTarget, request: ChatCompletionRequest) -> Iterator[str]:
        """
        Try each Codex account in LRU order.
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
            except CodexAuthError as exc:
                logger.warning("Codex account %s token refresh failed: %s", account.email, exc)
                last_err = exc
                continue

            try:
                yield from self._stream_from_account(account, target, request)
                return  # success — stop iterating accounts
            except ProviderRateLimitError as exc:
                logger.info("Codex account %s rate-limited, trying next account…", account.email)
                last_err = exc
                continue

        # All accounts exhausted
        raise ProviderRateLimitError(
            f"All {len(accounts)} Codex account(s) are rate-limited or unavailable. "
            f"Last error: {last_err}"
        )

    def _stream_from_account(self, account, target: ModelTarget, request: ChatCompletionRequest) -> Iterator[str]:
        payload = self._build_payload(target, request)
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "codex-cli/1.0.0",
            "Accept": "text/event-stream",
        }
        if account.account_id:
            headers["ChatGPT-Account-Id"] = account.account_id

        try:
            with self.tls_client.stream("POST", RESPONSES_URL, headers=headers, json=payload) as response:
                if response.status_code == 401:
                    raise ProviderAuthError(f"Codex authentication failed for {account.email}")
                if response.status_code == 429:
                    raise ProviderRateLimitError(f"Codex rate limit reached for {account.email}")
                if response.status_code >= 400:
                    raise ProviderRequestError(self._response_error(response))

                for event in iter_sse_json(response.iter_lines()):
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if delta:
                            yield delta
                    elif event_type in {"error", "response.failed"}:
                        raise ProviderRequestError(json.dumps(event, ensure_ascii=False))
        except TLSClientDependencyError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    def _build_payload(self, target: ModelTarget, request: ChatCompletionRequest) -> dict[str, Any]:
        # Separate system messages → go into "instructions" field (required by the endpoint)
        system_text, messages = split_system_messages(request.messages)
        if not messages:
            raise ProviderRequestError("At least one non-system message is required")

        input_items = [self._convert_message(msg) for msg in messages]

        payload: dict[str, Any] = {
            "model": target.model_id,
            "instructions": system_text or "You are a helpful assistant.",
            "input": input_items,
            "stream": True,
            "store": False,     # required by chatgpt.com/backend-api/codex/responses
        }
        # NOTE: chatgpt.com/backend-api/codex/responses does NOT accept
        # temperature / top_p — sending them returns HTTP 400.
        if request.max_tokens is not None:
            payload["max_output_tokens"] = request.max_tokens
        return payload

    @staticmethod
    def _convert_message(message: ChatMessage) -> dict[str, Any]:
        role = "assistant" if message.role == "assistant" else "user"
        return {
            "role": role,
            "content": [
                {
                    "type": "input_text",
                    "text": content_to_text(message.content),
                }
            ],
        }

    @staticmethod
    def _response_error(response: Any) -> str:
        try:
            # For streaming responses, read available text
            for line in response.iter_lines():
                line_str = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                if line_str.strip():
                    return f"Codex request failed: HTTP {response.status_code} {line_str}"
        except Exception:
            pass
        return f"Codex request failed: HTTP {response.status_code}"
