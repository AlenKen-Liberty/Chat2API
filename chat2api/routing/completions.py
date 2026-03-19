"""
/v1/chat/completions handler.

Routing logic:
  1. Resolve requested model → primary provider + model_id
  2. Try primary provider backend
  3. ProviderRateLimitError (429) → try cross-provider fallback
     e.g. Gemini exhausted → gpt-5.4 on Codex
  4. Both exhausted → 503
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from chat2api.models.openai_types import ChatCompletionRequest
from chat2api.models.tiers import ModelTarget, UnknownModelError, get_model_router
from chat2api.protocol.converter import (
    build_chat_completion_chunk,
    build_chat_completion_response,
    degradation_headers,
    new_completion_id,
)
from chat2api.protocol.sse import encode_sse
from chat2api.providers import get_backend_registry
from chat2api.providers.base import ProviderError, ProviderRateLimitError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["completions"])


async def _call_backend(target: ModelTarget, request: ChatCompletionRequest):
    """
    Call the appropriate backend for target.
    Returns (stream_iterator | text_str, actual_model_id).
    Propagates ProviderError subclasses.
    """
    backend = get_backend_registry().get(target.provider)
    if request.stream:
        return backend.stream_text(target, request), target.model_id
    return backend.generate_text(target, request), target.model_id


async def _execute_with_fallback(request: ChatCompletionRequest):
    """
    Try primary provider; on rate-limit, fall back to the other provider.

    Returns:
        (result, actual_model_id, requested_name, degraded_reason)
    """
    model_router = get_model_router()
    target = model_router.resolve(request.model)

    # ── Try primary provider ──
    try:
        result, actual_model_id = await _call_backend(target, request)
        return result, actual_model_id, target.requested_name, ""

    except ProviderRateLimitError as primary_err:
        logger.warning(
            "Provider '%s' rate-limited for model '%s' (%s). Trying fallback '%s'...",
            target.provider, target.model_id, primary_err, target.fallback_provider,
        )

    # ── Try cross-provider fallback ──
    fallback = model_router.resolve_fallback(target)
    reason = f"{target.provider}-{target.quota_group}-exhausted"

    try:
        result, actual_model_id = await _call_backend(fallback, request)
        return result, actual_model_id, target.requested_name, reason

    except ProviderRateLimitError as fallback_err:
        logger.error(
            "Fallback provider '%s' also exhausted (%s). All providers unavailable.",
            fallback.provider, fallback_err,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": (
                        f"All providers exhausted. "
                        f"'{target.provider}' and '{fallback.provider}' are both rate-limited. "
                        "Please try again later."
                    ),
                    "type": "service_unavailable",
                }
            },
        )


@router.post("/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest):
    # ── Validate model name early for a clean 400 ──
    try:
        get_model_router().resolve(request.model)
    except UnknownModelError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": exc.message, "type": "invalid_request_error"}},
        ) from exc

    # ── Execute with cross-provider fallback ──
    try:
        result, actual_model_id, requested_name, reason = await _execute_with_fallback(request)
    except HTTPException:
        raise
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"message": str(exc), "type": exc.__class__.__name__}},
        ) from exc

    headers = degradation_headers(
        requested_model=requested_name,
        actual_model=actual_model_id,
        reason=reason,
    )

    if request.stream:
        completion_id = new_completion_id()
        stream = _openai_stream(
            result,  # type: ignore[arg-type]
            model=actual_model_id,
            completion_id=completion_id,
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    payload = build_chat_completion_response(model=actual_model_id, content=result)  # type: ignore[arg-type]
    return JSONResponse(content=payload, headers=headers)


async def _openai_stream(chunks: Iterator[str], model: str, completion_id: str) -> AsyncIterator[str]:
    try:
        for chunk in chunks:
            if chunk:
                yield encode_sse(
                    build_chat_completion_chunk(
                        model=model,
                        delta=chunk,
                        completion_id=completion_id,
                    )
                )
        yield encode_sse(
            build_chat_completion_chunk(
                model=model,
                finish_reason="stop",
                completion_id=completion_id,
            )
        )
        yield encode_sse("[DONE]")
    except ProviderError as exc:
        yield encode_sse({"error": {"message": str(exc), "type": exc.__class__.__name__}})
        yield encode_sse("[DONE]")
