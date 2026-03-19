from __future__ import annotations

from fastapi import APIRouter, HTTPException

from chat2api.models.tiers import get_model_router


router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models")
async def list_models() -> dict:
    model_router = get_model_router()
    return {
        "object": "list",
        "data": [card.model_dump() for card in model_router.to_model_cards()],
    }


@router.get("/models/{model_id:path}")
async def get_model(model_id: str) -> dict:
    model_router = get_model_router()
    target = model_router.get(model_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return {
        "id": target.requested_name,
        "object": "model",
        "owned_by": target.provider,
        "provider_model_id": target.model_id,
        "quota_group": target.quota_group,
        "fallback_provider": target.fallback_provider,
        "fallback_model_id": target.fallback_model_id,
    }
