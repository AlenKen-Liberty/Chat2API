"""
/v1/chat/completions handler.

Routing logic:
  1. Resolve requested model → primary provider + model_id
  2. Try primary provider backend
  3. ProviderRateLimitError (429) → try cross-provider fallback
     e.g. Gemini exhausted → gpt-5.4 on Codex → gpt-4o on Copilot → llama on Groq
  4. All exhausted → 503
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

from fastapi import APIRouter, HTTPException, Request
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
from chat2api.providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from chat2api.usage_logger import UsageTimer, log_usage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["completions"])


async def _call_backend(target: ModelTarget, request: ChatCompletionRequest):
    backend = get_backend_registry().get(target.provider)
    if request.stream:
        return backend.stream_text(target, request), target.model_id
    return backend.generate_text(target, request), target.model_id


async def _execute_with_fallback(request: ChatCompletionRequest):
    """
    Try primary provider; on rate-limit, walk the fallback chain.
    The chain is: gemini → codex → copilot → groq → gemini (circular).
    We stop after visiting MAX_FALLBACKS providers to avoid infinite loops.
    """
    model_router = get_model_router()
    target = model_router.resolve(request.model)

    MAX_FALLBACKS = 4  # max distinct providers to try
    visited: set[str] = set()
    current = target

    while len(visited) < MAX_FALLBACKS:
        visited.add(current.provider)
        try:
            result, actual_model_id = await _call_backend(current, request)
            reason = "" if current is target else f"{target.provider}-{target.quota_group}-exhausted"
            return result, actual_model_id, target.requested_name, reason, current.provider
        except (ProviderRateLimitError, ProviderAuthError, ProviderUnavailableError) as err:
            logger.warning(
                "Provider '%s' rate-limited for '%s' (%s). Trying fallback '%s'…",
                current.provider, current.model_id, err, current.fallback_provider,
            )
            if current.fallback_provider in visited:
                break
            current = model_router.resolve_fallback(current)

    raise HTTPException(
        status_code=503,
        detail={
            "error": {
                "message": (
                    f"All providers exhausted ({', '.join(visited)}). "
                    "Please try again later."
                ),
                "type": "service_unavailable",
            }
        },
    )


@router.post("/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest, raw_request: Request):
    # ── Validate model name early for a clean 400 ──
    try:
        get_model_router().resolve(request.model)
    except UnknownModelError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": exc.message, "type": "invalid_request_error"}},
        ) from exc

    caller_ip = raw_request.client.host if raw_request.client else "unknown"
    timer = UsageTimer()

    # ── Execute with cross-provider fallback chain ──
    try:
        result, actual_model_id, requested_name, reason, provider = await _execute_with_fallback(request)
    except HTTPException as exc:
        timer.stop()
        log_usage(
            caller_ip=caller_ip,
            requested_model=request.model,
            actual_model="",
            provider="",
            degraded=False,
            duration_ms=timer.duration_ms,
            status="error",
            error=str(exc.detail),
            stream=request.stream,
        )
        raise
    except ProviderError as exc:
        timer.stop()
        log_usage(
            caller_ip=caller_ip,
            requested_model=request.model,
            actual_model="",
            provider="",
            degraded=False,
            duration_ms=timer.duration_ms,
            status="error",
            error=str(exc),
            stream=request.stream,
        )
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

        def on_stream_done() -> None:
            timer.stop()
            log_usage(
                caller_ip=caller_ip,
                requested_model=requested_name,
                actual_model=actual_model_id,
                provider=provider,
                degraded=bool(reason),
                duration_ms=timer.duration_ms,
                stream=True,
            )

        return StreamingResponse(
            _logged_stream(stream, on_stream_done),
            media_type="text/event-stream",
            headers=headers,
        )

    timer.stop()
    log_usage(
        caller_ip=caller_ip,
        requested_model=requested_name,
        actual_model=actual_model_id,
        provider=provider,
        degraded=bool(reason),
        duration_ms=timer.duration_ms,
        stream=False,
    )

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


async def _logged_stream(stream: AsyncIterator[str], on_done) -> AsyncIterator[str]:
    """Wraps an async stream to call on_done when iteration finishes."""
    try:
        async for chunk in stream:
            yield chunk
    finally:
        on_done()
