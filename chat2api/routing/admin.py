from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from chat2api.account.copilot_account import (
    CopilotAuthError,
    PREMIUM_REQUEST_RESET_NOTE as COPILOT_PREMIUM_RESET_NOTE,
    get_copilot_account,
    ensure_fresh_account as ensure_fresh_copilot_account,
    get_active_account_email as get_active_copilot_account_email,
)
from chat2api.account.codex_account import (
    CodexAuthError,
    ensure_fresh_account as ensure_fresh_codex_account,
    get_active_account_email as get_active_codex_account_email,
    list_accounts as list_codex_accounts,
    save_account as save_codex_account,
    set_active_account as set_active_codex_account,
)
from chat2api.account.gemini_account import (
    GeminiAuthError,
    ensure_fresh_account as ensure_fresh_gemini_account,
    get_active_account_email as get_active_gemini_account_email,
    list_accounts as list_gemini_accounts,
    set_active_account as set_active_gemini_account,
)
from chat2api.config import get_settings
from chat2api.providers.openai_compat import describe_api_keys
from chat2api.quota import (
    CODEX_USAGE_URL,
    RETRIEVE_USER_QUOTA_URL,
    QuotaFetchError,
    fetch_codex_usage,
    fetch_gemini_project_info,
    fetch_gemini_quota,
    format_iso_reset_time,
    format_unix_reset_time,
    percent,
    remaining_percent_from_used,
    unix_reset_time_to_iso,
)


router = APIRouter(prefix="/admin", tags=["admin"])

PROVIDER_ORDER = ("codex", "gemini", "copilot", "groq")
COPILOT_PREMIUM_MODEL_MULTIPLIERS = {
    "claude-haiku-4.5": 0.33,
    "claude-opus-4.1": 10.0,
    "claude-opus-4.5": 3.0,
    "claude-sonnet-4": 1.0,
    "claude-sonnet-4.5": 1.0,
    "gemini-2.5-pro": 1.0,
    "gemini-3-flash": 0.33,
    "gemini-3-pro": 1.0,
    "gpt-4.1": 0.0,
    "gpt-4o": 0.0,
    "gpt-5": 1.0,
    "gpt-5 mini": 0.0,
    "gpt-5-codex": 1.0,
    "gpt-5.1": 1.0,
    "gpt-5.1-codex": 1.0,
    "gpt-5.1-codex-mini": 0.33,
    "grok-code-fast-1": 0.25,
    "raptor mini": 0.0,
}


def _provider_display_name(provider_name: str) -> str:
    return {
        "codex": "Codex",
        "gemini": "Gemini",
        "copilot": "GitHub Copilot",
        "groq": "Groq",
    }.get(provider_name, provider_name.title())


@router.get("/health")
async def admin_health() -> dict:
    gemini_accounts = list_gemini_accounts()
    codex_accounts = list_codex_accounts()
    copilot_account = _load_copilot_dashboard_account()
    groq_keys = describe_api_keys("groq")
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
            "copilot": {
                "accounts": 1 if copilot_account.get("configured") else 0,
                "enabled_accounts": 1 if copilot_account.get("configured") else 0,
            },
            "groq": {
                "accounts": groq_keys["key_count"],
                "enabled_accounts": groq_keys["key_count"],
            },
        },
    }


@router.get("/quota-urls", name="admin_quota_urls")
async def admin_quota_urls(request: Request, provider: str | None = None, fresh: int = 0):
    if provider:
        use_cache = not fresh
        entry = _build_single_provider_entry(request, provider, use_cache=use_cache)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown provider '{provider}'")
        if _wants_html(request):
            return HTMLResponse(_render_provider_fragment(entry))
        return entry
    if _wants_html(request):
        provider_names = _available_provider_names()
        return HTMLResponse(_render_quota_urls_html_tabbed(request, provider_names))
    payload = {
        "providers": _build_provider_entries(request),
    }
    return payload


@router.get("/quota", name="admin_quota")
async def admin_quota(request: Request, provider: str, account: str, model: str):
    model_entry = _get_model_entry(provider, model)

    if provider == "gemini":
        payload = _load_gemini_quota(account, model_entry)
    elif provider == "codex":
        payload = _load_codex_quota(account, model_entry)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider}'")

    if _wants_html(request):
        return HTMLResponse(_render_quota_detail_html(payload))
    return payload


@router.post("/activate-account", name="admin_activate_account")
async def admin_activate_account(request: Request, provider: str, account: str, next: str | None = None):
    if provider == "gemini":
        set_active_gemini_account(account)
    elif provider == "codex":
        set_active_codex_account(account)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider}'")

    target = next or str(request.url_for("admin_quota_urls"))
    return RedirectResponse(target, status_code=303)


def _wants_html(request: Request) -> bool:
    output_format = request.query_params.get("format")
    if output_format == "json":
        return False
    if output_format == "html":
        return True
    return "text/html" in request.headers.get("accept", "")


def _configured_models() -> dict[str, list[dict[str, Any]]]:
    settings = get_settings()
    grouped: dict[str, dict[str, dict[str, Any]]] = {}

    for alias, entry in settings.models.items():
        provider_models = grouped.setdefault(entry.provider, {})
        current = provider_models.setdefault(
            entry.model_id,
            {
                "model_id": entry.model_id,
                "aliases": [],
                "quota_group": settings.providers[entry.provider].quota_group,
            },
        )
        current["aliases"].append(alias)

    result: dict[str, list[dict[str, Any]]] = {}
    for provider, items in grouped.items():
        result[provider] = sorted(
            (
                {
                    "model_id": item["model_id"],
                    "aliases": sorted(item["aliases"]),
                    "quota_group": item["quota_group"],
                }
                for item in items.values()
            ),
            key=lambda item: item["model_id"],
        )
    return result


def _get_model_entry(provider: str, model_id: str) -> dict[str, Any]:
    for entry in _configured_models().get(provider, []):
        if entry["model_id"] == model_id:
            return entry
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found for provider '{provider}'")


def _provider_quota_group(provider_name: str) -> str | None:
    provider = get_settings().providers.get(provider_name)
    return provider.quota_group if provider else None


def _provider_usage_note(provider_name: str) -> str | None:
    if provider_name == "gemini":
        return (
            "Gemini cards group models by the live quota bucket returned by retrieveUserQuota. "
            "When multiple models currently share the same remaining percentage and reset time, they collapse into one pool."
        )
    if provider_name == "codex":
        return (
            "Codex exposes one shared account-level quota pool. The upstream usage payload also exposes a shorter "
            "secondary window when present, plus a separate code-review window."
        )
    if provider_name == "copilot":
        return (
            "GitHub Copilot uses GitHub OAuth. On paid plans and Copilot Student, GPT-4o, GPT-4.1, and GPT-5 mini "
            "are included chat models and do not consume premium requests; premium models consume the monthly "
            "premium pool using their per-model multiplier. Premium request counters reset on the 1st of each month "
            "at 00:00 UTC."
        )
    if provider_name == "groq":
        return (
            "Groq uses API keys. This tab shows local key wiring from .env or ~/.chat2api/keys/groq.txt and the "
            "configured model map; it does not fetch live account usage from Groq."
        )
    return None


def _build_provider_entries(request: Request) -> list[dict[str, Any]]:
    entries = []
    for provider_name in _available_provider_names():
        entry = _build_single_provider_entry(request, provider_name, use_cache=False)
        if entry is not None:
            entries.append(entry)
    return entries


def _available_provider_names() -> list[str]:
    settings = get_settings()
    names = [name for name in PROVIDER_ORDER if settings.providers.get(name)]
    return names or list(PROVIDER_ORDER)


def _build_single_provider_entry(
    request: Request, provider_name: str, *, use_cache: bool = False,
) -> dict[str, Any] | None:
    models = _configured_models()
    if provider_name == "gemini":
        active_email = get_active_gemini_account_email()
        builder = _build_gemini_account_entry_cached if use_cache else _build_gemini_account_entry
        return {
            "provider": "gemini",
            "quota_group": _provider_quota_group("gemini"),
            "shared_quota": False,
            "usage_note": _provider_usage_note("gemini"),
            "accounts": [
                builder(request, account, models.get("gemini", []), active_email=active_email)
                for account in list_gemini_accounts()
            ],
        }
    if provider_name == "codex":
        active_email = get_active_codex_account_email()
        builder = _build_codex_account_entry_cached if use_cache else _build_codex_account_entry
        return {
            "provider": "codex",
            "quota_group": _provider_quota_group("codex"),
            "shared_quota": True,
            "usage_note": _provider_usage_note("codex"),
            "accounts": [
                builder(request, account, models.get("codex", []), active_email=active_email)
                for account in list_codex_accounts()
            ],
        }
    if provider_name == "copilot":
        builder = _build_copilot_provider_entry_cached if use_cache else _build_copilot_provider_entry
        return builder(request, models.get("copilot", []))
    if provider_name == "groq":
        return _build_groq_provider_entry(request, models.get("groq", []))
    return None


def _load_copilot_dashboard_account() -> dict[str, Any]:
    try:
        account = ensure_fresh_copilot_account()
    except CopilotAuthError as exc:
        return {
            "configured": False,
            "display_name": "GitHub Copilot OAuth",
            "email": "github-copilot",
            "plan_name": "Plan unavailable",
            "sku": None,
            "api_base": None,
            "auth_mode": "GitHub OAuth",
            "premium_requests_per_month": None,
            "premium_usage": None,
            "quota_error": str(exc),
        }

    return {
        "configured": True,
        "display_name": account.username or "GitHub Copilot",
        "email": account.email,
        "plan_name": account.plan_name,
        "sku": account.sku,
        "api_base": account.api_base,
        "auth_mode": account.auth_mode,
        "premium_requests_per_month": account.premium_requests_per_month,
        "premium_usage": account.premium_usage,
        "quota_error": None,
    }


def _load_copilot_dashboard_account_cached() -> dict[str, Any]:
    try:
        # get_copilot_account() will lazy load the long-lived token but won't trigger session API 
        # unless session_token is accessed
        account = get_copilot_account()
        # If no session has been fetched yet, usage is empty
        usage = account._premium_usage
        
        return {
            "configured": True,
            "display_name": account.username or "GitHub Copilot",
            "email": account.email,
            "plan_name": account.plan_name,
            "sku": account.sku,
            "api_base": account._api_base,  # direct property access to avoid network
            "auth_mode": account.auth_mode,
            "premium_requests_per_month": account.premium_requests_per_month,
            "premium_usage": usage,
            "quota_error": None if account._session_expires_at > 0 else "No cached session (click Refresh)",
        }
    except CopilotAuthError as exc:
        return {
            "configured": False,
            "display_name": "GitHub Copilot OAuth",
            "email": "github-copilot",
            "plan_name": "Plan unavailable",
            "sku": None,
            "api_base": None,
            "auth_mode": "GitHub OAuth",
            "premium_requests_per_month": None,
            "premium_usage": None,
            "quota_error": str(exc),
        }


def _copilot_model_policy(model_id: str) -> dict[str, Any]:
    multiplier = COPILOT_PREMIUM_MODEL_MULTIPLIERS.get(model_id)
    if multiplier is None:
        return {
            "kind": "unknown",
            "multiplier": None,
            "multiplier_label": "unknown",
            "description": "Not listed in the current GitHub model-multiplier docs.",
        }
    if float(multiplier) == 0.0:
        return {
            "kind": "included",
            "multiplier": multiplier,
            "multiplier_label": "included",
            "description": "Included model on paid plans and Copilot Student.",
        }
    return {
        "kind": "premium",
        "multiplier": multiplier,
        "multiplier_label": f"{multiplier:g}x",
        "description": "Consumes premium requests using the documented multiplier.",
    }


def _build_copilot_provider_entry(
    request: Request, provider_models: list[dict[str, Any]], *, cached: bool = False
) -> dict[str, Any]:
    account = _load_copilot_dashboard_account_cached() if cached else _load_copilot_dashboard_account()
    active_email = get_active_copilot_account_email()

    model_entries = []
    included_models: list[str] = []
    premium_models: list[str] = []
    unknown_models: list[str] = []
    for model in provider_models:
        policy = _copilot_model_policy(model["model_id"])
        enriched = {
            **model,
            "policy": policy,
        }
        model_entries.append(enriched)
        if policy["kind"] == "included":
            included_models.append(model["model_id"])
        elif policy["kind"] == "premium":
            premium_models.append(f"{model['model_id']} ({policy['multiplier_label']})")
        else:
            unknown_models.append(model["model_id"])

    premium_requests = account.get("premium_requests_per_month")
    premium_usage = account.get("premium_usage")
    plan_is_free = premium_requests == 50 and str(account.get("plan_name")).startswith("Copilot Free")
    included_summary = "Counts toward premium" if plan_is_free else "Unlimited"
    included_meta = (
        "Copilot Free counts chat requests against the premium request pool."
        if plan_is_free
        else "Paid plans and Copilot Student include GPT-4o / GPT-4.1 / GPT-5 mini chat without premium-request spend."
    )

    # Build premium summary — prefer live usage %, fall back to entitlement
    if premium_usage and premium_usage.get("usage_percent") is not None:
        pct = premium_usage["usage_percent"]
        premium_summary = f"{pct}%"
        limit = premium_usage.get("limit") or premium_requests
        used = premium_usage.get("used")
        if used is not None and limit:
            premium_meta_text = f"{used} / {limit} premium requests used. {COPILOT_PREMIUM_RESET_NOTE}."
        else:
            premium_meta_text = (
                f"{pct}% of premium requests used"
                f" ({limit}/month entitlement)." if limit else "."
            ) + f" {COPILOT_PREMIUM_RESET_NOTE}."
    elif isinstance(premium_requests, int):
        premium_summary = f"{premium_requests}/month"
        premium_meta_text = f"Premium models use per-model multipliers. {COPILOT_PREMIUM_RESET_NOTE}."
    else:
        premium_summary = "Plan-dependent"
        premium_meta_text = f"Premium models use per-model multipliers. {COPILOT_PREMIUM_RESET_NOTE}."

    return {
        "provider": "copilot",
        "quota_group": _provider_quota_group("copilot"),
        "shared_quota": True,
        "usage_note": _provider_usage_note("copilot"),
        "accounts": [
            {
                "email": str(account["email"]),
                "display_name": str(account["display_name"]),
                "disabled": False,
                "is_active": str(account["email"]) == active_email or active_email is None,
                "meta_items": [
                    "GitHub OAuth",
                    str(account["plan_name"]),
                    f"sku={account['sku']}" if account.get("sku") else None,
                    account.get("api_base"),
                ],
                "quota_error": account.get("quota_error"),
                "status_badge": "GitHub OAuth",
                "models": model_entries,
                "included_models": included_models,
                "premium_models": premium_models,
                "unknown_models": unknown_models,
                "included_summary": included_summary,
                "included_meta": included_meta,
                "premium_summary": premium_summary,
                "premium_meta": premium_meta_text,
                "auth_summary": str(account["plan_name"]),
                "auth_meta": "OAuth token -> Copilot session token exchange via GitHub.",
            }
        ],
    }


def _build_copilot_provider_entry_cached(request: Request, provider_models: list[dict[str, Any]]) -> dict[str, Any]:
    entry = _build_copilot_provider_entry(request, provider_models, cached=True)
    entry["accounts"][0]["cached"] = True
    return entry


def _build_groq_provider_entry(request: Request, provider_models: list[dict[str, Any]]) -> dict[str, Any]:
    key_info = describe_api_keys("groq")
    source_summary = ", ".join(key_info["sources"]) if key_info["sources"] else "No key source detected"
    return {
        "provider": "groq",
        "quota_group": _provider_quota_group("groq"),
        "shared_quota": True,
        "usage_note": _provider_usage_note("groq"),
        "accounts": [
            {
                "email": "groq",
                "display_name": "Groq API keys",
                "disabled": not key_info["configured"],
                "is_active": key_info["configured"],
                "meta_items": [
                    "OpenAI-compatible API",
                    "https://api.groq.com/openai/v1",
                ],
                "quota_error": (
                    None
                    if key_info["configured"]
                    else "No Groq API key configured. Set GROQ_API_KEY in .env or ~/.chat2api/keys/groq.txt."
                ),
                "status_badge": "API key",
                "models": provider_models,
                "keys_summary": str(key_info["key_count"]),
                "keys_meta": source_summary,
                "config_summary": "Ready" if key_info["configured"] else "Missing",
                "config_meta": "Environment variables are loaded from .env at startup.",
                "masked_keys": key_info["masked_keys"],
            }
        ],
    }


def _build_codex_account_entry_cached(
    request: Request,
    account: Any,
    provider_models: list[dict[str, Any]],
    *,
    active_email: str | None,
) -> dict[str, Any]:
    """Build codex account entry using cached quota_snapshot (no API calls)."""
    entry = _base_account_entry(request, "codex", account, provider_models, shared_quota=True, active_email=active_email)
    if account.disabled:
        entry["disabled_reason"] = getattr(account, "disabled_reason", None)
        entry["quota_error"] = "Account is disabled"
        return entry

    entry["account_id"] = account.account_id
    entry["plan_type"] = account.plan_type
    entry["cached"] = True

    snapshot = account.quota_snapshot
    if not snapshot:
        entry["quota_error"] = "No cached quota (click Refresh)"
        return entry

    entry["plan_type"] = snapshot.get("plan_type") or account.plan_type
    quota_summary = {
        "weekly": _codex_window((snapshot.get("rate_limit") or {}).get("primary_window") or {}),
        "burst": _codex_window((snapshot.get("rate_limit") or {}).get("secondary_window") or {}),
        "code_review": _codex_window((snapshot.get("code_review_rate_limit") or {}).get("primary_window") or {}),
    }
    entry["quota"] = quota_summary
    return entry


def _build_gemini_account_entry_cached(
    request: Request,
    account: Any,
    provider_models: list[dict[str, Any]],
    *,
    active_email: str | None,
) -> dict[str, Any]:
    """Build gemini account entry using cached quota (no API calls)."""
    entry = _base_account_entry(request, "gemini", account, provider_models, shared_quota=False, active_email=active_email)
    entry["disabled"] = account.disabled
    entry["disabled_reason"] = getattr(account, "disabled_reason", None)
    entry["project_id"] = getattr(account, "project_id", None)
    entry["subscription_tier"] = getattr(account, "subscription_tier", None)
    entry["cached"] = True

    cached_quota = getattr(account, "quota", None) or {}
    cached_models = cached_quota.get("models") or []
    if not cached_models:
        entry["quota_error"] = "No cached quota (click Refresh)"
        return entry

    pct_by_model = {m["name"]: m.get("pct") for m in cached_models if m.get("name")}

    for model in entry["models"]:
        pct = pct_by_model.get(model["model_id"])
        if pct is None:
            model["quota_error"] = "Not in cache"
            continue
        model["quota"] = {
            "remaining_fraction": pct / 100.0 if pct is not None else None,
            "remaining_percent": float(pct) if pct is not None else None,
            "reset_time": None,
            "reset_in": None,
        }

    entry["groups"] = _group_gemini_models(entry["models"])
    return entry


def _build_gemini_account_entry(
    request: Request,
    account: Any,
    provider_models: list[dict[str, Any]],
    *,
    active_email: str | None,
) -> dict[str, Any]:
    entry = _base_account_entry(request, "gemini", account, provider_models, shared_quota=False, active_email=active_email)

    try:
        account = ensure_fresh_gemini_account(account)
    except GeminiAuthError as exc:
        entry["disabled"] = account.disabled
        entry["disabled_reason"] = account.disabled_reason
        entry["quota_error"] = str(exc)
        return entry

    entry["disabled"] = account.disabled
    entry["disabled_reason"] = account.disabled_reason
    project_id = account.project_id
    subscription_tier = account.subscription_tier

    if not project_id:
        try:
            project_id, subscription_tier = fetch_gemini_project_info(account.token.access_token)
        except QuotaFetchError as exc:
            entry["quota_error"] = str(exc)
            return entry

    entry["project_id"] = project_id
    entry["subscription_tier"] = subscription_tier

    try:
        quota_data = fetch_gemini_quota(account.token.access_token, project_id)
    except QuotaFetchError as exc:
        entry["quota_error"] = str(exc)
        return entry

    buckets = quota_data.get("buckets") or []
    buckets_by_model = {bucket.get("modelId"): bucket for bucket in buckets if bucket.get("modelId")}

    for model in entry["models"]:
        bucket = buckets_by_model.get(model["model_id"])
        if not bucket:
            model["quota_error"] = "Quota bucket not returned by Gemini"
            continue
        remaining_fraction = bucket.get("remainingFraction")
        model["quota"] = {
            "remaining_fraction": remaining_fraction,
            "remaining_percent": percent(remaining_fraction),
            "reset_time": bucket.get("resetTime"),
            "reset_in": format_iso_reset_time(bucket.get("resetTime")),
        }

    entry["groups"] = _group_gemini_models(entry["models"])
    return entry


def _build_codex_account_entry(
    request: Request,
    account: Any,
    provider_models: list[dict[str, Any]],
    *,
    active_email: str | None,
) -> dict[str, Any]:
    entry = _base_account_entry(request, "codex", account, provider_models, shared_quota=True, active_email=active_email)
    if account.disabled:
        entry["disabled_reason"] = getattr(account, "disabled_reason", None)
        entry["quota_error"] = "Account is disabled"
        return entry

    try:
        account = ensure_fresh_codex_account(account)
    except CodexAuthError as exc:
        entry["disabled_reason"] = getattr(account, "disabled_reason", None)
        entry["quota_error"] = str(exc)
        return entry

    entry["account_id"] = account.account_id
    entry["plan_type"] = account.plan_type

    try:
        usage = fetch_codex_usage(account.access_token, account.account_id)
    except QuotaFetchError as exc:
        entry["quota_error"] = str(exc)
        return entry

    entry["plan_type"] = usage.get("plan_type") or account.plan_type
    quota_summary = {
        "weekly": _codex_window((usage.get("rate_limit") or {}).get("primary_window") or {}),
        "burst": _codex_window((usage.get("rate_limit") or {}).get("secondary_window") or {}),
        "code_review": _codex_window((usage.get("code_review_rate_limit") or {}).get("primary_window") or {}),
    }
    entry["quota"] = quota_summary

    # Persist snapshot for instant cache loads
    try:
        account.quota_snapshot = usage
        save_codex_account(account)
    except Exception:
        pass

    return entry


def _base_account_entry(
    request: Request,
    provider: str,
    account: Any,
    provider_models: list[dict[str, Any]],
    *,
    shared_quota: bool,
    active_email: str | None,
) -> dict[str, Any]:
    quota_url = None
    if provider_models:
        quota_url = _quota_url(request, provider=provider, account=account.email, model=provider_models[0]["model_id"])

    return {
        "email": account.email,
        "disabled": account.disabled,
        "disabled_reason": getattr(account, "disabled_reason", None),
        "project_id": getattr(account, "project_id", None),
        "subscription_tier": getattr(account, "subscription_tier", None),
        "account_id": getattr(account, "account_id", None),
        "plan_type": getattr(account, "plan_type", None),
        "quota_url": quota_url,
        "activate_url": _activate_account_url(request, provider=provider, account=account.email),
        "is_active": account.email == active_email,
        "models": [
            {
                "model_id": model["model_id"],
                "aliases": model["aliases"],
                "quota_group": model["quota_group"],
                "url": _quota_url(
                    request,
                    provider=provider,
                    account=account.email,
                    model=model["model_id"],
                ),
                "shared_quota": shared_quota,
            }
            for model in provider_models
        ],
    }


def _quota_url(request: Request, provider: str, account: str, model: str) -> str:
    base = str(request.url_for("admin_quota"))
    query = urlencode({"provider": provider, "account": account, "model": model})
    return f"{base}?{query}"


def _activate_account_url(request: Request, provider: str, account: str) -> str:
    base = str(request.url_for("admin_activate_account"))
    query = urlencode(
        {
            "provider": provider,
            "account": account,
            "next": str(request.url_for("admin_quota_urls")),
        }
    )
    return f"{base}?{query}"


def _load_gemini_quota(account_email: str, model_entry: dict[str, Any]) -> dict[str, Any]:
    account = next((item for item in list_gemini_accounts() if item.email == account_email), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Gemini account '{account_email}' not found")

    try:
        account = ensure_fresh_gemini_account(account)
    except GeminiAuthError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to refresh Gemini account '{account_email}': {exc}",
        ) from exc

    project_id = account.project_id
    subscription_tier = account.subscription_tier

    if not project_id:
        try:
            project_id, subscription_tier = fetch_gemini_project_info(account.token.access_token)
        except QuotaFetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        quota_data = fetch_gemini_quota(account.token.access_token, project_id)
    except QuotaFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    buckets = quota_data.get("buckets") or []
    bucket = next((item for item in buckets if item.get("modelId") == model_entry["model_id"]), None)
    if bucket is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Gemini quota bucket for model '{model_entry['model_id']}' "
                f"was not returned for account '{account_email}'"
            ),
        )

    remaining_fraction = bucket.get("remainingFraction")
    shared_models = sorted(
        item.get("modelId", "")
        for item in buckets
        if item.get("remainingFraction") == remaining_fraction and item.get("resetTime") == bucket.get("resetTime")
    )

    return {
        "provider": "gemini",
        "account": {
            "email": account.email,
            "project_id": project_id,
            "subscription_tier": subscription_tier,
        },
        "model": {
            "id": model_entry["model_id"],
            "aliases": model_entry["aliases"],
            "quota_group": model_entry["quota_group"],
            "shared_quota_models": shared_models,
        },
        "quota": {
            "remaining_fraction": remaining_fraction,
            "remaining_percent": percent(remaining_fraction),
            "reset_time": bucket.get("resetTime"),
            "reset_in": format_iso_reset_time(bucket.get("resetTime")),
            "raw_bucket": bucket,
        },
        "upstream": {
            "method": "POST",
            "url": RETRIEVE_USER_QUOTA_URL,
            "project_id": project_id,
        },
    }


def _load_codex_quota(account_email: str, model_entry: dict[str, Any]) -> dict[str, Any]:
    account = next((item for item in list_codex_accounts() if item.email == account_email), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Codex account '{account_email}' not found")

    try:
        account = ensure_fresh_codex_account(account)
    except CodexAuthError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to refresh Codex account '{account_email}': {exc}",
        ) from exc

    try:
        usage = fetch_codex_usage(account.access_token, account.account_id)
    except QuotaFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    rate_limit = usage.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}
    code_review = (usage.get("code_review_rate_limit") or {}).get("primary_window") or {}
    all_codex_models = [item["model_id"] for item in _configured_models().get("codex", [])]

    return {
        "provider": "codex",
        "account": {
            "email": account.email,
            "account_id": account.account_id,
            "plan_type": usage.get("plan_type") or account.plan_type,
        },
        "model": {
            "id": model_entry["model_id"],
            "aliases": model_entry["aliases"],
            "quota_group": model_entry["quota_group"],
            "shared_quota_models": all_codex_models,
            "shared_quota_note": "Codex quota is shared across all configured Codex models.",
        },
        "quota": {
            "weekly": _codex_window(primary),
            "burst": _codex_window(secondary),
            "code_review": _codex_window(code_review),
            "raw_usage": usage,
        },
        "upstream": {
            "method": "GET",
            "url": CODEX_USAGE_URL,
            "account_id": account.account_id,
        },
    }


def _codex_window(window: dict[str, Any]) -> dict[str, Any]:
    used_percent = window.get("used_percent")
    reset_at = window.get("reset_at")
    return {
        "used_percent": used_percent,
        "remaining_percent": remaining_percent_from_used(used_percent),
        "reset_at": reset_at,
        "reset_time": unix_reset_time_to_iso(reset_at),
        "reset_in": format_unix_reset_time(reset_at),
    }


def _group_gemini_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for model in models:
        key = _gemini_group_key(model)
        group = grouped.setdefault(
            key,
            {
                "key": key,
                "label": "",
                "models": [],
                "quota": None,
                "mixed_quota": False,
                "quota_error": None,
            },
        )
        group["models"].append(model)

        quota = model.get("quota") or {}
        if model.get("quota_error") and not group.get("quota_error"):
            group["quota_error"] = model["quota_error"]

        if quota and group.get("quota") is None:
            group["quota"] = quota

    result = list(grouped.values())
    for group in result:
        group["label"] = _gemini_group_label(group)

    return sorted(result, key=_gemini_group_sort_key)


def _gemini_group_key(model: dict[str, Any]) -> str:
    quota = model.get("quota") or {}
    remaining = quota.get("remaining_fraction")
    if remaining is None:
        remaining = quota.get("remaining_percent")
    reset_time = quota.get("reset_time")
    if remaining is not None or reset_time:
        return f"live:{remaining}:{reset_time or ''}"

    quota_group = model.get("quota_group")
    if quota_group:
        return f"configured:{quota_group}"

    return f"model:{model['model_id']}"


def _gemini_group_label(group: dict[str, Any]) -> str:
    models = group.get("models") or []
    model_count = len(models)
    families = sorted(
        {
            family
            for family in (_gemini_family(model["model_id"]) for model in models)
            if family != "other"
        }
    )
    key = str(group.get("key") or "")

    if key.startswith("configured:"):
        quota_group = key.split(":", 1)[1]
        base = "Shared pool" if quota_group == "gemini-all" else f"Quota pool {quota_group}"
    elif model_count == 1:
        base = models[0]["model_id"]
    elif len(families) == 1:
        base = f"{families[0].title()} pool"
    else:
        base = "Shared pool"

    if model_count > 1:
        return f"{base} ({model_count} models)"
    return base


def _gemini_group_sort_key(group: dict[str, Any]) -> tuple[int, float, str]:
    order = {"shared": 0, "pro": 1, "flash": 2, "lite": 3, "other": 4}
    families = {
        _gemini_family(model["model_id"])
        for model in group.get("models") or []
    }
    if len(families) == 1:
        family = next(iter(families))
        family_order = order.get(family, 99)
    else:
        family_order = order["shared"]
    model_ids = sorted(model["model_id"] for model in group.get("models") or [])
    first_model_id = model_ids[0] if model_ids else str(group.get("label") or "")
    return (family_order, first_model_id)


def _gemini_family(model_id: str) -> str:
    value = (model_id or "").lower()
    if "lite" in value:
        return "lite"
    if "pro" in value:
        return "pro"
    if "flash" in value:
        return "flash"
    return "other"


def _provider_sort_key(provider: dict[str, Any]) -> tuple[int, str]:
    name = str(provider.get("provider") or "")
    order = {provider_name: index for index, provider_name in enumerate(PROVIDER_ORDER)}
    return (order.get(name, 99), name)


def _render_provider_fragment(provider: dict[str, Any]) -> str:
    """Render a single provider section as an HTML fragment (no <html> wrapper)."""
    provider_name = provider["provider"]
    provider_class = f"provider--{escape(provider_name)}"
    sections = []
    sections.append(f"<section class='provider {provider_class}'>")
    sections.append("<div class='provider__head'>")
    sections.append(
        f"<div><div class='provider__label'>{escape(_provider_display_name(provider_name))}</div>"
        f"<div class='provider__meta'>{escape(_provider_summary(provider_name))}</div></div>"
    )
    sections.append(f"<div class='provider__count'>{len(provider.get('accounts') or [])} account(s)</div>")
    sections.append("</div>")
    if provider.get("usage_note"):
        sections.append(f"<div class='provider__note'>{escape(str(provider['usage_note']))}</div>")

    accounts = provider.get("accounts") or []
    if not accounts:
        sections.append("<div class='empty'>No accounts found.</div>")
        sections.append("</section>")
        return "".join(sections)

    sections.append("<div class='account-list'>")
    for account in sorted(accounts, key=_account_sort_key):
        if provider_name == "gemini":
            sections.append(_render_gemini_account_card(account))
        elif provider_name == "codex":
            sections.append(_render_codex_account_card(account))
        elif provider_name == "copilot":
            sections.append(_render_copilot_account_card(account))
        else:
            sections.append(_render_groq_account_card(account))
    sections.append("</div></section>")
    return "".join(sections)


def _render_quota_urls_html_tabbed(request: Request, provider_names: list[str]) -> str:
    """Render the tabbed shell page with lazy-loading JavaScript."""
    base_url = str(request.url_for("admin_quota_urls"))
    sections = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>Chat2API Quota Dashboard</title>",
        f"<style>{_dashboard_styles()}</style>",
        "<style>",
        ".tab-bar { display: flex; gap: 6px; margin-bottom: 16px; }",
        ".tab-btn { appearance: none; border: 1px solid var(--line); background: var(--soft); "
        "border-radius: 999px; padding: 10px 20px; font: inherit; font-size: 15px; font-weight: 600; "
        "cursor: pointer; text-transform: capitalize; color: var(--muted); transition: all .15s; }",
        ".tab-btn:hover { background: rgba(24,34,48,0.08); }",
        ".tab-btn--active { background: var(--ink); color: white; border-color: var(--ink); }",
        ".tab-btn--codex.tab-btn--active { background: var(--codex); border-color: var(--codex); }",
        ".tab-btn--gemini.tab-btn--active { background: var(--gemini); border-color: var(--gemini); }",
        ".tab-btn--copilot.tab-btn--active { background: var(--copilot); border-color: var(--copilot); }",
        ".tab-btn--groq.tab-btn--active { background: var(--groq); border-color: var(--groq); }",
        ".tab-pane { display: none; }",
        ".tab-pane--active { display: block; }",
        ".tab-loading { padding: 40px; text-align: center; color: var(--muted); font-size: 15px; }",
        ".tab-error { padding: 20px; color: var(--bad); background: rgba(180,35,24,0.06); border-radius: 16px; }",
        ".refresh-btn { appearance: none; border: 1px solid var(--line); background: var(--soft); "
        "border-radius: 999px; padding: 8px 16px; font: inherit; font-size: 13px; cursor: pointer; "
        "color: var(--muted); margin-left: auto; }",
        ".refresh-btn:hover { background: rgba(24,34,48,0.08); }",
        ".tab-header { display: flex; align-items: center; gap: 10px; }",
        "</style>",
        "</head><body>",
        "<div class='shell'>",
        "<header class='hero'>",
        "<div class='hero__eyebrow'>Chat2API Admin</div>",
        "<h1>Quota Dashboard</h1>",
        "<p>Click a provider tab to load its quota data. Click Refresh to reload.</p>",
        "</header>",
        "<div class='tab-header'>",
        "<div class='tab-bar'>",
    ]

    for i, name in enumerate(provider_names):
        active = " tab-btn--active" if i == 0 else ""
        sections.append(
            f"<button class='tab-btn tab-btn--{escape(name)}{active}' "
            f"data-tab='{escape(name)}' onclick='switchTab(\"{escape(name)}\")'>"
            f"{escape(_provider_display_name(name))}</button>"
        )

    sections.append("</div>")
    sections.append("<button class='refresh-btn' onclick='refreshTab()'>Refresh</button>")
    sections.append("</div>")

    for i, name in enumerate(provider_names):
        active = " tab-pane--active" if i == 0 else ""
        sections.append(f"<div class='tab-pane{active}' id='tab-{escape(name)}'>"
                        "<div class='tab-loading'>Loading...</div></div>")

    sections.append(f"""
<script>
const BASE = {json.dumps(base_url)};
const cached = {{}};
const fresh = {{}};
let activeTab = {json.dumps(provider_names[0] if provider_names else '')};

async function loadTab(name, force) {{
  if (!force && fresh[name]) return;
  const pane = document.getElementById('tab-' + name);
  if (!pane) return;

  // Phase 1: instant cached render (skip if we already have fresh data or are forcing)
  if (!cached[name] && !force) {{
    pane.innerHTML = "<div class='tab-loading'>Loading " + name + "...</div>";
    try {{
      const cResp = await fetch(BASE + '?provider=' + encodeURIComponent(name) + '&format=html');
      if (cResp.ok) {{
        pane.innerHTML = await cResp.text();
        cached[name] = true;
      }}
    }} catch (e) {{}}
  }}

  // Phase 2: fresh data in background
  if (force) {{
    pane.insertAdjacentHTML('beforeend',
      "<div class='tab-refreshing' style='text-align:center;padding:8px;color:var(--muted);font-size:13px'>Refreshing...</div>");
  }}
  try {{
    const fResp = await fetch(BASE + '?provider=' + encodeURIComponent(name) + '&fresh=1&format=html');
    if (fResp.ok) {{
      pane.innerHTML = await fResp.text();
      fresh[name] = true;
      cached[name] = true;
    }}
  }} catch (err) {{
    if (!cached[name]) {{
      pane.innerHTML = "<div class='tab-error'>Failed to load " + name + ": " + err.message + "</div>";
    }}
    const r = pane.querySelector('.tab-refreshing');
    if (r) r.remove();
  }}
}}

function switchTab(name) {{
  activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(b => {{
    b.classList.toggle('tab-btn--active', b.dataset.tab === name);
  }});
  document.querySelectorAll('.tab-pane').forEach(p => {{
    p.classList.toggle('tab-pane--active', p.id === 'tab-' + name);
  }});
  loadTab(name, false);
}}

function refreshTab() {{
  if (activeTab) {{
    fresh[activeTab] = false;
    loadTab(activeTab, true);
  }}
}}

// Auto-load first tab
if (activeTab) loadTab(activeTab, false);
</script>
""")

    sections.append("</div></body></html>")
    return "".join(sections)


def _render_quota_urls_html(payload: dict[str, Any]) -> str:
    sections = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>Chat2API Quota Dashboard</title>",
        f"<style>{_dashboard_styles()}</style>",
        "</head><body>",
        "<div class='shell'>",
        "<header class='hero'>",
        "<div class='hero__eyebrow'>Chat2API Admin</div>",
        "<h1>Quota Dashboard</h1>",
        "<p>Main board only shows quota, reset time, active state, and account switching. "
        "Click an account or quota block when you want the detailed view.</p>",
        "</header>",
    ]

    for provider in sorted(payload["providers"], key=_provider_sort_key):
        provider_name = provider["provider"]
        provider_class = f"provider--{escape(provider_name)}"
        sections.append(f"<section class='provider {provider_class}'>")
        sections.append("<div class='provider__head'>")
        sections.append(
            f"<div><div class='provider__label'>{escape(_provider_display_name(provider_name))}</div>"
            f"<div class='provider__meta'>{escape(_provider_summary(provider_name))}</div></div>"
        )
        sections.append(f"<div class='provider__count'>{len(provider.get('accounts') or [])} account(s)</div>")
        sections.append("</div>")
        if provider.get("usage_note"):
            sections.append(f"<div class='provider__note'>{escape(str(provider['usage_note']))}</div>")

        accounts = provider.get("accounts") or []
        if not accounts:
            sections.append("<div class='empty'>No accounts found.</div>")
            sections.append("</section>")
            continue

        sections.append("<div class='account-list'>")
        for account in sorted(accounts, key=_account_sort_key):
            if provider_name == "gemini":
                sections.append(_render_gemini_account_card(account))
            elif provider_name == "codex":
                sections.append(_render_codex_account_card(account))
            elif provider_name == "copilot":
                sections.append(_render_copilot_account_card(account))
            else:
                sections.append(_render_groq_account_card(account))
        sections.append("</div></section>")

    sections.append("</div></body></html>")
    return "".join(sections)


def _display_value(value: Any, *, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value}{suffix}"


def _provider_summary(provider_name: str) -> str:
    if provider_name == "codex":
        return "Each row shows the shared weekly Codex pool for that account."
    if provider_name == "gemini":
        return "Each row shows the live Gemini quota pools for that account."
    if provider_name == "copilot":
        return "Shows GitHub OAuth status, included-model policy, and the shared monthly premium-request pool."
    if provider_name == "groq":
        return "Shows Groq API-key wiring from local env/file config plus the routed model set."
    return "Per-account quota overview."


def _account_sort_key(account: dict[str, Any]) -> tuple[int, int, int, float, float, str]:
    # Extract primary remaining percent for sorting
    remaining = -1.0  # unknown/error sorts last
    reset_at = float("inf")
    quota = account.get("quota") or {}
    if quota:
        # Codex: shared weekly quota
        weekly = quota.get("weekly") or {}
        rp = weekly.get("remaining_percent")
        if isinstance(rp, (int, float)):
            remaining = rp
        ra = weekly.get("reset_at")
        if isinstance(ra, (int, float)):
            reset_at = ra
    else:
        # Gemini: use the lowest remaining across groups
        for group in account.get("groups") or []:
            gq = group.get("quota") or {}
            rp = gq.get("remaining_percent")
            if isinstance(rp, (int, float)):
                if remaining < 0 or rp < remaining:
                    remaining = rp

    return (
        0 if account.get("is_active") else 1,
        0 if not account.get("quota_error") else 1,
        0 if not account.get("disabled") else 1,
        -remaining,   # higher quota first (negate for ascending sort)
        reset_at,     # earlier reset first
        str(account.get("email") or ""),
    )


def _render_provider_overview(provider: dict[str, Any]) -> str:
    provider_name = provider["provider"]
    models = _configured_models().get(provider_name, [])
    sections = ["<div class='provider-overview'>"]

    if provider_name == "codex":
        sections.append("<div class='provider-overview__label'>Available models</div>")
        sections.append(_render_model_pills(models, tone="codex"))
    else:
        sections.append("<div class='provider-overview__label'>Configured quota pools</div>")
        sections.append("<div class='catalog-grid'>")
        grouped = _group_gemini_models(models)
        for group in grouped:
            sections.append("<div class='catalog-card'>")
            sections.append(f"<div class='catalog-card__title'>{escape(group['label'])}</div>")
            sections.append(_render_model_pills(group.get("models") or [], tone="gemini"))
            sections.append("</div>")
        sections.append("</div>")

    sections.append("</div>")
    return "".join(sections)


def _render_model_pills(models: list[dict[str, Any]], *, tone: str) -> str:
    sections = [f"<div class='model-pill-list model-pill-list--{escape(tone)}'>"]
    for model in models:
        sections.append(f"<span class='model-pill model-pill--{escape(tone)}'>{escape(model['model_id'])}</span>")
    sections.append("</div>")
    return "".join(sections)


def _dashboard_styles() -> str:
    return """
    :root {
      --bg: #f3efe7;
      --panel: rgba(255, 252, 246, 0.92);
      --panel-strong: #fffdf8;
      --ink: #182230;
      --muted: #667085;
      --line: rgba(24, 34, 48, 0.12);
      --shadow: 0 18px 48px rgba(31, 41, 55, 0.08);
      --good: #117864;
      --warn: #b26a00;
      --bad: #b42318;
      --good-bg: rgba(17, 120, 100, 0.12);
      --warn-bg: rgba(178, 106, 0, 0.14);
      --bad-bg: rgba(180, 35, 24, 0.12);
      --soft: rgba(24, 34, 48, 0.04);
      --gemini: #0c8f7a;
      --codex: #c55a11;
      --copilot: #2563eb;
      --groq: #d9485f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(12, 143, 122, 0.10), transparent 28%),
        radial-gradient(circle at top right, rgba(197, 90, 17, 0.10), transparent 30%),
        linear-gradient(180deg, #fbf8f2 0%, var(--bg) 100%);
    }
    a { color: inherit; text-decoration: none; }
    .shell {
      width: min(1380px, calc(100vw - 32px));
      margin: 24px auto 40px;
    }
    .hero {
      padding: 28px 30px;
      border-radius: 28px;
      background: linear-gradient(135deg, rgba(255,255,255,0.88), rgba(255,250,241,0.92));
      box-shadow: var(--shadow);
      border: 1px solid rgba(255,255,255,0.65);
      margin-bottom: 20px;
    }
    .hero__eyebrow {
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .hero h1 {
      margin: 0 0 10px;
      font-size: clamp(30px, 4vw, 44px);
      line-height: 1;
    }
    .hero p {
      margin: 0;
      max-width: 980px;
      color: var(--muted);
      line-height: 1.6;
    }
    .provider {
      margin-top: 18px;
      padding: 20px;
      border-radius: 26px;
      background: var(--panel);
      box-shadow: var(--shadow);
      border: 1px solid rgba(255,255,255,0.72);
      backdrop-filter: blur(10px);
    }
    .provider__head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
      margin-bottom: 10px;
    }
    .provider__label {
      font-size: 26px;
      font-weight: 700;
      text-transform: capitalize;
    }
    .provider__meta, .provider__note {
      color: var(--muted);
      line-height: 1.5;
    }
    .provider__count {
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(24, 34, 48, 0.06);
      font-size: 13px;
      white-space: nowrap;
    }
    .provider--gemini .provider__label { color: var(--gemini); }
    .provider--codex .provider__label { color: var(--codex); }
    .provider--copilot .provider__label { color: var(--copilot); }
    .provider--groq .provider__label { color: var(--groq); }
    .account-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-top: 14px;
    }
    .account-row {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(0, 2.4fr) auto;
      gap: 14px;
      align-items: center;
      border-radius: 20px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
      padding: 14px 16px;
      min-width: 0;
    }
    .account-row__identity {
      min-width: 0;
    }
    .account-row__title {
      font-size: 17px;
      font-weight: 700;
      word-break: break-word;
    }
    .account-row__title a {
      color: inherit;
      text-decoration: none;
    }
    .account-row__meta {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .account-row__meta a {
      color: inherit;
      text-decoration: underline;
      text-decoration-color: rgba(24, 34, 48, 0.18);
      text-underline-offset: 2px;
    }
    .account-row__action {
      display: flex;
      flex-direction: column;
      gap: 8px;
      align-items: flex-end;
      justify-self: end;
      flex-shrink: 0;
    }
    .quota-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      min-width: 0;
    }
    .quota-brief {
      min-width: 148px;
      flex: 1 1 160px;
      border-radius: 16px;
      border: 1px solid var(--line);
      padding: 10px 12px;
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(250,246,239,0.88));
    }
    .quota-brief--link {
      display: block;
      color: inherit;
      text-decoration: none;
    }
    .quota-brief--error {
      background: rgba(180, 35, 24, 0.05);
      border-color: rgba(180, 35, 24, 0.18);
    }
    .quota-brief__head {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: baseline;
    }
    .quota-brief__label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .quota-brief__value {
      font-size: 22px;
      font-weight: 700;
      line-height: 1;
    }
    .quota-brief__value--text {
      font-size: 18px;
      line-height: 1.2;
      word-break: break-word;
    }
    .quota-brief__meta {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 10px 0 14px;
    }
    .chip {
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(24, 34, 48, 0.05);
      font-size: 12px;
      color: var(--muted);
    }
    .chip--good { background: var(--good-bg); color: var(--good); }
    .chip--warn { background: var(--warn-bg); color: var(--warn); }
    .chip--bad { background: var(--bad-bg); color: var(--bad); }
    .chip--solid {
      color: white;
      background: linear-gradient(135deg, var(--good), #0b5f4e);
    }
    .chip--active {
      color: white;
      background: linear-gradient(135deg, var(--good), #0b5f4e);
    }
    .chip--action {
      color: white;
      background: linear-gradient(135deg, #c55a11, #8f3b08);
    }
    .chip-button {
      appearance: none;
      border: 0;
      cursor: pointer;
      font: inherit;
    }
    .chip-button:hover {
      filter: brightness(0.97);
    }
    .alert {
      border-radius: 16px;
      padding: 12px 14px;
      margin-bottom: 14px;
      font-size: 13px;
      line-height: 1.5;
      border: 1px solid rgba(180, 35, 24, 0.18);
      background: rgba(180, 35, 24, 0.08);
      color: var(--bad);
    }
    .account-details {
      margin-top: 14px;
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .account-details summary {
      display: flex;
      justify-content: space-between;
      align-items: center;
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      color: var(--muted);
      list-style: none;
    }
    .account-details summary::after {
      content: "+";
      font-size: 18px;
      line-height: 1;
      color: var(--muted);
    }
    .account-details[open] summary::after { content: "−"; }
    .account-details summary::-webkit-details-marker { display: none; }
    .account-details__body {
      margin-top: 12px;
    }
    .detail-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }
    .detail-link {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 12px;
      border: 1px solid var(--line);
      background: rgba(24, 34, 48, 0.04);
      color: var(--ink);
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric {
      border-radius: 18px;
      border: 1px solid var(--line);
      padding: 14px;
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(250,246,239,0.88));
    }
    .metric__label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .metric__value {
      margin-top: 10px;
      font-size: 28px;
      font-weight: 700;
      line-height: 1;
    }
    .tone-good { color: var(--good); }
    .tone-warn { color: var(--warn); }
    .tone-bad { color: var(--bad); }
    .tone-neutral { color: var(--ink); }
    .metric__meta {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.5;
    }
    .table-wrap {
      overflow-x: auto;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.68);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      text-align: left;
    }
    tr:last-child td { border-bottom: 0; }
    th {
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: rgba(24, 34, 48, 0.04);
    }
    .model-name {
      font-weight: 700;
      color: var(--ink);
    }
    .group-name {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 700;
      color: var(--ink);
    }
    .group-badge {
      padding: 5px 8px;
      border-radius: 999px;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: rgba(12, 143, 122, 0.12);
      color: var(--gemini);
    }
    .model-alias {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      line-height: 1.4;
    }
    .meter {
      margin-top: 8px;
      height: 8px;
      border-radius: 999px;
      background: rgba(24, 34, 48, 0.08);
      overflow: hidden;
    }
    .meter > span {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #1ea97a, #8dc63f);
    }
    .meter--warn > span { background: linear-gradient(90deg, #c57b1a, #efb84c); }
    .meter--bad > span { background: linear-gradient(90deg, #c1471d, #e5484d); }
    .mono {
      font-family: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
    }
    .codex-models {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 6px;
    }
    .codex-models a {
      border-radius: 999px;
      padding: 7px 10px;
      font-size: 12px;
      background: rgba(197, 90, 17, 0.10);
      color: var(--codex);
    }
    .gemini-models {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .gemini-models a {
      border-radius: 999px;
      padding: 7px 10px;
      font-size: 12px;
      background: rgba(12, 143, 122, 0.10);
      color: var(--gemini);
    }
    .note {
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 16px;
      background: var(--soft);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }
    .empty {
      padding: 22px;
      border-radius: 18px;
      background: rgba(24, 34, 48, 0.04);
      color: var(--muted);
      text-align: center;
    }
    @media (max-width: 760px) {
      .shell { width: min(100vw - 18px, 1380px); }
      .hero, .provider, .account-row { padding-left: 14px; padding-right: 14px; }
      .provider__head, .account-row {
        grid-template-columns: 1fr;
        align-items: start;
      }
      .provider__head {
        display: flex;
        flex-direction: column;
        align-items: start;
      }
      .account-row__action {
        align-items: start;
        justify-self: start;
      }
      .quota-brief {
        min-width: 100%;
      }
    }
    """


def _render_gemini_account_card(account: dict[str, Any]) -> str:
    sections = ["<article class='account-row'>"]
    sections.append(_render_account_identity(account))
    if account.get("quota_error"):
        sections.append(_render_quota_error(str(account["quota_error"])))
    else:
        groups = account.get("groups") or []
        if groups:
            sections.append("<div class='quota-strip'>")
            for group in groups:
                group_url = next((model.get("url") for model in group.get("models") or [] if model.get("url")), None)
                sections.append(
                    _render_quota_brief(
                        _compact_group_label(group["label"]),
                        group.get("quota") or {},
                        href=group_url,
                    )
                )
            sections.append("</div>")
        else:
            sections.append(_render_quota_error("No Gemini quota groups returned for this account."))
    sections.append(_render_account_action(account, provider="gemini"))
    sections.append("</article>")
    return "".join(sections)


def _render_codex_account_card(account: dict[str, Any]) -> str:
    sections = ["<article class='account-row'>"]
    sections.append(_render_account_identity(account))
    if account.get("quota_error"):
        sections.append(_render_quota_error(str(account["quota_error"])))
    else:
        quota = account.get("quota") or {}
        sections.append(
            "<div class='quota-strip'>"
            + _render_quota_brief(
                "Weekly",
                quota.get("weekly") or {},
                href=account.get("quota_url"),
            )
            + "</div>"
        )
    sections.append(_render_account_action(account, provider="codex"))
    sections.append("</article>")
    return "".join(sections)


def _render_copilot_account_card(account: dict[str, Any]) -> str:
    sections = ["<article class='account-row'>"]
    sections.append(_render_account_identity(account))
    if account.get("quota_error"):
        sections.append(_render_quota_error(str(account["quota_error"])))
    else:
        sections.append("<div class='quota-strip'>")
        sections.append(
            _render_info_brief(
                "Included Models",
                account.get("included_summary"),
                account.get("included_meta"),
                tone="good",
            )
        )
        sections.append(
            _render_info_brief(
                "Premium Pool",
                account.get("premium_summary"),
                account.get("premium_meta"),
                tone="warn",
            )
        )
        sections.append(
            _render_info_brief(
                "Configured Models",
                _display_model_policy_summary(account),
                _display_model_policy_meta(account),
                tone="neutral",
            )
        )
        sections.append("</div>")
    sections.append(_render_static_account_action(account.get("status_badge") or "GitHub OAuth", tone="solid"))
    sections.append("</article>")
    return "".join(sections)


def _render_groq_account_card(account: dict[str, Any]) -> str:
    sections = ["<article class='account-row'>"]
    sections.append(_render_account_identity(account))
    if account.get("quota_error"):
        sections.append(_render_quota_error(str(account["quota_error"])))
    else:
        sections.append("<div class='quota-strip'>")
        sections.append(
            _render_info_brief(
                "API Keys",
                account.get("keys_summary"),
                account.get("keys_meta"),
                tone="good",
            )
        )
        sections.append(
            _render_info_brief(
                "Config State",
                account.get("config_summary"),
                account.get("config_meta"),
                tone="neutral",
            )
        )
        sections.append(
            _render_info_brief(
                "Configured Models",
                str(len(account.get("models") or [])),
                ", ".join(model["model_id"] for model in account.get("models") or []) or "No models configured",
                tone="neutral",
            )
        )
        sections.append("</div>")
    sections.append(_render_static_account_action(account.get("status_badge") or "API key"))
    sections.append("</article>")
    return "".join(sections)


def _render_account_identity(account: dict[str, Any]) -> str:
    quota_url = account.get("quota_url")
    title = escape(str(account.get("display_name") or account["email"]))
    if quota_url and not account.get("quota_error"):
        title_html = f"<a href=\"{escape(quota_url)}\">{title}</a>"
    else:
        title_html = title
    meta_items = [str(item) for item in account.get("meta_items") or [] if item]
    if account.get("quota_error"):
        meta_items.append("Quota unavailable")
    meta = " · ".join(meta_items)
    meta_html = f"<div class='account-row__meta'>{escape(meta)}</div>" if meta else ""
    return (
        "<div class='account-row__identity'>"
        f"<div class='account-row__title'>{title_html}</div>"
        f"{meta_html}"
        "</div>"
    )


def _render_meta_chips(items: list[tuple[str, Any]]) -> str:
    chips = ["<div class='chips'>"]
    for key, value in items:
        chips.append(f"<div class='chip'>{escape(key)}={escape(_display_value(value))}</div>")
    chips.append("</div>")
    return "".join(chips)


def _render_account_action(account: dict[str, Any], *, provider: str) -> str:
    if account.get("is_active"):
        return "<div class='account-row__action'><div class='chip chip--active'>Active account</div></div>"

    return (
        "<div class='account-row__action'>"
        "<form method='post' "
        f"action=\"{escape(account['activate_url'])}\">"
        "<button type='submit' class='chip chip--action chip-button'>Become active account</button>"
        "</form>"
        "</div>"
    )


def _render_static_account_action(label: str, *, tone: str = "solid") -> str:
    class_name = "chip chip--solid" if tone == "solid" else "chip"
    return f"<div class='account-row__action'><div class='{class_name}'>{escape(label)}</div></div>"


def _render_quota_brief(label: str, quota: dict[str, Any], *, href: str | None = None) -> str:
    wrapper_tag = "a" if href else "div"
    wrapper_attrs = (
        f" class='quota-brief quota-brief--link' href=\"{escape(href)}\""
        if href
        else " class='quota-brief'"
    )
    title = escape(label)
    remaining = quota.get("remaining_percent")
    reset_in = _display_value(quota.get("reset_in"))
    reset_time = _compact_reset_time(quota.get("reset_time"))
    return (
        f"<{wrapper_tag}{wrapper_attrs}>"
        "<div class='quota-brief__head'>"
        f"<div class='quota-brief__label'>{title}</div>"
        f"<div class='quota-brief__value {escape(_tone_class(remaining))}'>{escape(_display_value(remaining, suffix='%'))}</div>"
        "</div>"
        f"<div class='quota-brief__meta'>Resets in {escape(reset_in)}<br><span class='mono'>{escape(reset_time)}</span></div>"
        f"</{wrapper_tag}>"
    )


def _render_info_brief(label: str, value: Any, meta: Any, *, tone: str = "neutral") -> str:
    tone_class = {
        "good": "tone-good",
        "warn": "tone-warn",
        "bad": "tone-bad",
    }.get(tone, "tone-neutral")
    meta_text = escape(str(meta)) if meta is not None else "N/A"
    return (
        "<div class='quota-brief'>"
        "<div class='quota-brief__head'>"
        f"<div class='quota-brief__label'>{escape(label)}</div>"
        f"<div class='quota-brief__value quota-brief__value--text {tone_class}'>{escape(_display_value(value))}</div>"
        "</div>"
        f"<div class='quota-brief__meta'>{meta_text}</div>"
        "</div>"
    )


def _render_quota_error(message: str) -> str:
    return (
        "<div class='quota-strip'>"
        "<div class='quota-brief quota-brief--error'>"
        "<div class='quota-brief__head'>"
        "<div class='quota-brief__label'>Quota unavailable</div>"
        "<div class='quota-brief__value tone-bad'>N/A</div>"
        "</div>"
        f"<div class='quota-brief__meta'>{escape(message)}</div>"
        "</div>"
        "</div>"
    )


def _compact_group_label(label: str) -> str:
    value = str(label or "")
    if " (" in value:
        value = value.split(" (", 1)[0]
    if value.endswith(" pool"):
        value = value[:-5]
    return value


def _display_model_policy_summary(account: dict[str, Any]) -> str:
    included = len(account.get("included_models") or [])
    premium = len(account.get("premium_models") or [])
    unknown = len(account.get("unknown_models") or [])
    parts = [f"{included} included", f"{premium} premium"]
    if unknown:
        parts.append(f"{unknown} unknown")
    return " / ".join(parts)


def _display_model_policy_meta(account: dict[str, Any]) -> str:
    chunks = []
    if account.get("included_models"):
        chunks.append("Included: " + ", ".join(account["included_models"]))
    if account.get("premium_models"):
        chunks.append("Premium: " + ", ".join(account["premium_models"]))
    if account.get("unknown_models"):
        chunks.append("Unknown-docs: " + ", ".join(account["unknown_models"]))
    return " | ".join(chunks) or "No configured models"


def _compact_reset_time(value: Any) -> str:
    text = _display_value(value)
    if text == "N/A" or "T" not in text:
        return text
    date_part, time_part = text.split("T", 1)
    suffix = "Z" if time_part.endswith("Z") else ""
    time_core = time_part[:-1] if suffix else time_part
    hhmm = ":".join(time_core.split(":")[:2])
    return f"{date_part} {hhmm}{suffix}"


def _render_account_details(summary_label: str, body_html: str) -> str:
    return (
        "<details class='account-details'>"
        f"<summary>{escape(summary_label)}</summary>"
        f"<div class='account-details__body'>{body_html}</div>"
        "</details>"
    )


def _render_detail_links(items: list[tuple[str, str]]) -> str:
    links = ["<div class='detail-links'>"]
    for label, href in items:
        links.append(f"<a class='detail-link' href=\"{escape(href)}\">{escape(label)}</a>")
    links.append("</div>")
    return "".join(links)


def _render_window_metric(label: str, window: dict[str, Any], description: str | None = None) -> str:
    remaining = window.get("remaining_percent")
    meta = []
    if description:
        meta.append(escape(description))
    meta.append(f"reset_time={escape(_display_value(window.get('reset_time')))}")
    meta.append(f"reset_in={escape(_display_value(window.get('reset_in')))}")
    return (
        "<div class='metric'>"
        f"<div class='metric__label'>{escape(label)}</div>"
        f"<div class='metric__value {escape(_tone_class(remaining))}'>{escape(_display_value(remaining, suffix='%'))}</div>"
        f"<div class='metric__meta'>{'<br>'.join(meta)}</div>"
        "</div>"
    )


def _chip_class(remaining: Any) -> str:
    if not isinstance(remaining, (int, float)):
        return "chip"
    if remaining >= 60:
        return "chip chip--good"
    if remaining >= 25:
        return "chip chip--warn"
    return "chip chip--bad"


def _meter_class(remaining: Any) -> str:
    if not isinstance(remaining, (int, float)):
        return ""
    if remaining >= 60:
        return ""
    if remaining >= 25:
        return "meter--warn"
    return "meter--bad"


def _meter_width(remaining: Any) -> int:
    if not isinstance(remaining, (int, float)):
        return 0
    return max(0, min(100, int(round(float(remaining)))))


def _tone_class(remaining: Any) -> str:
    if not isinstance(remaining, (int, float)):
        return "tone-neutral"
    if remaining >= 60:
        return "tone-good"
    if remaining >= 25:
        return "tone-warn"
    return "tone-bad"


def _render_quota_detail_html(payload: dict[str, Any]) -> str:
    model = payload["model"]
    account = payload["account"]
    quota = payload["quota"]

    sections = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>Chat2API Quota Detail</title>",
        f"<style>{_dashboard_styles()}</style>",
        "</head><body><div class='shell'>",
        "<header class='hero'>",
        f"<div class='hero__eyebrow'>{escape(payload['provider'])} quota detail</div>",
        f"<h1>{escape(account['email'])} · {escape(model['id'])}</h1>",
        "<p>Add <span class='mono'>?format=json</span> to this URL if you want the raw JSON response.</p>",
        "</header>",
        "<section class='provider'>",
        "<div class='account-card'>",
        _render_meta_chips(
            [
                ("aliases", ", ".join(model.get("aliases") or [])),
                ("quota_group", model.get("quota_group")),
                ("provider", payload.get("provider")),
            ]
        ),
    ]

    if payload["provider"] == "gemini":
        sections.append(
            _render_meta_chips(
                [
                    ("project_id", account.get("project_id")),
                    ("subscription_tier", account.get("subscription_tier")),
                ]
            )
        )
        sections.append("<div class='metrics'>")
        sections.append(
            "<div class='metric'>"
            "<div class='metric__label'>Remaining</div>"
            f"<div class='metric__value {escape(_tone_class(quota.get('remaining_percent')))}'>{escape(_display_value(quota.get('remaining_percent'), suffix='%'))}</div>"
            f"<div class='metric__meta'>reset_time={escape(_display_value(quota.get('reset_time')))}<br>"
            f"reset_in={escape(_display_value(quota.get('reset_in')))}</div>"
            "</div>"
        )
        sections.append(
            "<div class='metric'>"
            "<div class='metric__label'>Shared Bucket</div>"
            f"<div class='metric__value tone-neutral'>{escape(str(len(model.get('shared_quota_models') or [])))}</div>"
            f"<div class='metric__meta'>{escape(', '.join(model.get('shared_quota_models') or []))}</div>"
            "</div>"
        )
        sections.append("</div>")
    else:
        sections.append(
            _render_meta_chips(
                [
                    ("account_id", account.get("account_id")),
                    ("plan_type", account.get("plan_type")),
                ]
            )
        )
        sections.append("<div class='metrics'>")
        sections.append(
            _render_window_metric(
                "Weekly",
                quota.get("weekly") or {},
                "Long-window shared quota for normal Codex work across all models.",
            )
        )
        sections.append(
            _render_window_metric(
                "Burst",
                quota.get("burst") or {},
                "Short-window spike cap when OpenAI returns a secondary window.",
            )
        )
        sections.append(
            _render_window_metric(
                "Code Review",
                quota.get("code_review") or {},
                "Separate allowance for review-style Codex actions when exposed upstream.",
            )
        )
        sections.append("</div>")
        if model.get("shared_quota_note"):
            sections.append(f"<p class='provider__note'>{escape(str(model['shared_quota_note']))}</p>")
        sections.append(
            "<div class='note'>This page reflects the raw `wham/usage` response: `weekly` comes from "
            "`rate_limit.primary_window`, `burst` comes from `rate_limit.secondary_window`, and "
            "`code_review` comes from `code_review_rate_limit.primary_window`.</div>"
        )
        sections.append("<div class='account-card__subtitle'>Shared across models</div>")
        sections.append("<div class='codex-models'>")
        for item in model.get("shared_quota_models") or []:
            sections.append(f"<span class='chip'>{escape(item)}</span>")
        sections.append("</div>")

    sections.extend(
        [
            "<div class='account-card__subtitle' style='margin-top:14px'>Raw JSON</div>",
            f"<div class='table-wrap'><pre style='margin:0;padding:16px;overflow:auto' class='mono'>{escape(json.dumps(payload, indent=2, ensure_ascii=False))}</pre></div>",
            "</div></section></div></body></html>",
        ]
    )
    return "".join(sections)
