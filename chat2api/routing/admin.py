from __future__ import annotations

from fastapi import APIRouter

from chat2api.account.codex_account import list_accounts as list_codex_accounts
from chat2api.account.gemini_account import list_accounts as list_gemini_accounts


router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/health")
def admin_health() -> dict:
    gemini_accounts = list_gemini_accounts()
    codex_accounts = list_codex_accounts()
    return {
        "status": "healthy",
        "providers": {
            "gemini": {
                "accounts": len(gemini_accounts),
                "enabled_accounts": sum(1 for account in gemini_accounts if not account.disabled),
            },
            "codex": {
                "accounts": len(codex_accounts),
                "enabled_accounts": sum(1 for account in codex_accounts if not account.disabled),
            },
        },
    }
