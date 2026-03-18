from __future__ import annotations

import time
import uuid
from typing import Any


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def build_chat_completion_response(
    model: str,
    content: str,
    finish_reason: str = "stop",
    completion_id: str | None = None,
) -> dict[str, Any]:
    created = int(time.time())
    return {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def build_chat_completion_chunk(
    model: str,
    delta: str | None = None,
    finish_reason: str | None = None,
    completion_id: str | None = None,
) -> dict[str, Any]:
    created = int(time.time())
    chunk: dict[str, Any] = {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }
        ],
    }
    if delta:
        chunk["choices"][0]["delta"] = {"content": delta}
    return chunk


def degradation_headers(
    requested_model: str,
    actual_model: str,
    reason: str = "",
) -> dict[str, str]:
    """
    Response headers telling the caller what actually happened.

    X-Chat2API-Requested-Model: what the caller asked for
    X-Chat2API-Actual-Model:    what was actually used (model_id sent to provider)
    X-Chat2API-Degraded:        "true" if a cross-provider fallback occurred
    X-Chat2API-Degraded-Reason: e.g. "gemini-all-exhausted"
    """
    degraded = requested_model != actual_model
    headers: dict[str, str] = {
        "X-Chat2API-Requested-Model": requested_model,
        "X-Chat2API-Actual-Model": actual_model,
        "X-Chat2API-Degraded": str(degraded).lower(),
    }
    if degraded and reason:
        headers["X-Chat2API-Degraded-Reason"] = reason
    return headers
