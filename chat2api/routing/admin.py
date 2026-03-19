from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from chat2api.account.codex_account import (
    CodexAuthError,
    ensure_fresh_account as ensure_fresh_codex_account,
    list_accounts as list_codex_accounts,
)
from chat2api.account.gemini_account import (
    GeminiAuthError,
    ensure_fresh_account as ensure_fresh_gemini_account,
    list_accounts as list_gemini_accounts,
)
from chat2api.config import get_settings
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


@router.get("/quota-urls", name="admin_quota_urls")
def admin_quota_urls(request: Request):
    payload = {
        "providers": _build_provider_entries(request),
    }
    if _wants_html(request):
        return HTMLResponse(_render_quota_urls_html(payload))
    return payload


@router.get("/quota", name="admin_quota")
def admin_quota(request: Request, provider: str, account: str, model: str):
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


def _build_provider_entries(request: Request) -> list[dict[str, Any]]:
    settings = get_settings()
    models = _configured_models()
    return [
        {
            "provider": "gemini",
            "quota_group": settings.providers.get("gemini").quota_group if settings.providers.get("gemini") else None,
            "shared_quota": False,
            "usage_note": (
                "Gemini is grouped by Flash, Lite, and Pro families. "
                "Within each family, the current quota buckets share the same remaining percentage and reset time."
            ),
            "accounts": [
                _build_gemini_account_entry(request, account, models.get("gemini", []))
                for account in list_gemini_accounts()
            ],
        },
        {
            "provider": "codex",
            "quota_group": settings.providers.get("codex").quota_group if settings.providers.get("codex") else None,
            "shared_quota": True,
            "usage_note": (
                "Codex exposes one shared account-level quota pool. "
                "The upstream usage payload also exposes a shorter secondary window when present, plus a separate "
                "code-review window. OpenAI documents that model and task complexity change average credit cost, "
                "so stronger models can drain the same pool faster than mini variants."
            ),
            "accounts": [
                _build_codex_account_entry(request, account, models.get("codex", []))
                for account in list_codex_accounts()
            ],
        },
    ]


def _build_gemini_account_entry(request: Request, account: Any, provider_models: list[dict[str, Any]]) -> dict[str, Any]:
    entry = _base_account_entry(request, "gemini", account, provider_models, shared_quota=False)

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


def _build_codex_account_entry(request: Request, account: Any, provider_models: list[dict[str, Any]]) -> dict[str, Any]:
    entry = _base_account_entry(request, "codex", account, provider_models, shared_quota=True)
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

    return entry


def _base_account_entry(
    request: Request,
    provider: str,
    account: Any,
    provider_models: list[dict[str, Any]],
    *,
    shared_quota: bool,
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
        "models": [
            {
                "model_id": model["model_id"],
                "aliases": model["aliases"],
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
        family = _gemini_family(model["model_id"])
        group = grouped.setdefault(
            family,
            {
                "key": family,
                "label": family.title(),
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

        if not quota:
            continue

        current = group.get("quota")
        if current is None:
            group["quota"] = quota
            continue

        current_signature = (
            current.get("remaining_percent"),
            current.get("reset_time"),
            current.get("reset_in"),
        )
        quota_signature = (
            quota.get("remaining_percent"),
            quota.get("reset_time"),
            quota.get("reset_in"),
        )
        if current_signature != quota_signature:
            group["mixed_quota"] = True

    order = {"flash": 0, "lite": 1, "pro": 2, "other": 3}
    return sorted(grouped.values(), key=lambda item: (order.get(item["key"], 99), item["label"]))


def _gemini_family(model_id: str) -> str:
    value = (model_id or "").lower()
    if "lite" in value:
        return "lite"
    if "pro" in value:
        return "pro"
    if "flash" in value:
        return "flash"
    return "other"


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
        "<p>Codex is summarized per account because all Codex models share the same quota pool. "
        "Gemini is grouped by Flash, Lite, and Pro families because the returned quota buckets line up that way.</p>",
        "</header>",
    ]

    for provider in payload["providers"]:
        provider_name = provider["provider"]
        provider_class = f"provider--{escape(provider_name)}"
        sections.append(f"<section class='provider {provider_class}'>")
        sections.append("<div class='provider__head'>")
        sections.append(
            f"<div><div class='provider__label'>{escape(provider_name)}</div>"
            f"<div class='provider__meta'>quota_group={escape(str(provider.get('quota_group')))} | "
            f"shared_quota={escape(str(provider.get('shared_quota')))}</div></div>"
        )
        sections.append(f"<div class='provider__count'>{len(provider.get('accounts') or [])} account(s)</div>")
        sections.append("</div>")
        if provider.get("usage_note"):
            sections.append(f"<p class='provider__note'>{escape(str(provider['usage_note']))}</p>")

        accounts = provider.get("accounts") or []
        if not accounts:
            sections.append("<div class='empty'>No accounts found.</div>")
            sections.append("</section>")
            continue

        sections.append("<div class='account-grid'>")
        for account in accounts:
            if provider_name == "gemini":
                sections.append(_render_gemini_account_card(account))
            else:
                sections.append(_render_codex_account_card(account))
        sections.append("</div></section>")

    sections.append("</div></body></html>")
    return "".join(sections)


def _display_value(value: Any, *, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value}{suffix}"


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
    .account-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 16px;
      margin-top: 16px;
    }
    .account-card {
      border-radius: 22px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
      padding: 18px 18px 16px;
      min-width: 0;
    }
    .account-card__head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      margin-bottom: 12px;
    }
    .account-card__title {
      font-size: 20px;
      font-weight: 700;
      word-break: break-word;
    }
    .account-card__subtitle {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
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
      .hero, .provider, .account-card { padding-left: 14px; padding-right: 14px; }
      .provider__head, .account-card__head { flex-direction: column; align-items: start; }
    }
    """


def _render_gemini_account_card(account: dict[str, Any]) -> str:
    sections = ["<article class='account-card'>"]
    sections.append(_render_account_head(account, provider="gemini"))
    sections.append(
        _render_meta_chips(
            [
                ("disabled", account.get("disabled")),
                ("project_id", account.get("project_id")),
                ("subscription_tier", account.get("subscription_tier")),
            ]
        )
    )
    if account.get("quota_error"):
        sections.append(f"<div class='alert'>quota_error={escape(str(account['quota_error']))}</div>")

    sections.append("<div class='table-wrap'><table>")
    sections.append(
        "<thead><tr><th>Group</th><th>Models</th><th>Remaining</th><th>Reset Time</th><th>Reset In</th></tr></thead><tbody>"
    )
    for group in account.get("groups") or []:
        quota = group.get("quota") or {}
        remaining = quota.get("remaining_percent")
        meter_class = _meter_class(remaining)
        sections.append("<tr>")
        sections.append(
            "<td>"
            f"<div class='group-name'><span>{escape(group['label'])}</span>"
            f"<span class='group-badge'>shared bucket</span></div>"
            + (
                "<div class='model-alias'>multiple quota buckets detected inside this family</div>"
                if group.get("mixed_quota")
                else "<div class='model-alias'>models in this family currently share the same quota window</div>"
            )
            + "</td>"
        )
        sections.append("<td><div class='gemini-models'>")
        for model in group.get("models") or []:
            sections.append(f"<a href=\"{escape(model['url'])}\">{escape(model['model_id'])}</a>")
        sections.append("</div></td>")
        sections.append(
            "<td>"
            f"<div class='{escape(_chip_class(remaining))} chip'>{escape(_display_value(remaining, suffix='%'))}</div>"
            f"<div class='meter {escape(meter_class)}'><span style=\"width:{_meter_width(remaining)}%\"></span></div>"
            "</td>"
        )
        sections.append(f"<td class='mono'>{escape(_display_value(quota.get('reset_time')))}</td>")
        sections.append(f"<td>{escape(_display_value(quota.get('reset_in')))}</td>")
        sections.append("</tr>")
    sections.append("</tbody></table></div></article>")
    return "".join(sections)


def _render_codex_account_card(account: dict[str, Any]) -> str:
    sections = ["<article class='account-card'>"]
    sections.append(_render_account_head(account, provider="codex"))
    sections.append(
        _render_meta_chips(
            [
                ("disabled", account.get("disabled")),
                ("account_id", account.get("account_id")),
                ("plan_type", account.get("plan_type")),
            ]
        )
    )
    if account.get("quota_error"):
        sections.append(f"<div class='alert'>quota_error={escape(str(account['quota_error']))}</div>")
    else:
        quota = account.get("quota") or {}
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
        sections.append(
            "<div class='note'>`Weekly` is the main shared pool. `Burst` comes from the shorter "
            "secondary window when the upstream usage API returns one. `Code Review` is a "
            "separate review-specific window exposed by OpenAI's Codex usage endpoint.</div>"
        )

    sections.append("<div class='account-card__subtitle'>Available models</div>")
    sections.append("<div class='codex-models'>")
    for model in account.get("models") or []:
        sections.append(f"<a href=\"{escape(model['url'])}\">{escape(model['model_id'])}</a>")
    sections.append("</div></article>")
    return "".join(sections)


def _render_account_head(account: dict[str, Any], *, provider: str) -> str:
    quota_url = account.get("quota_url")
    title = escape(account["email"])
    if quota_url:
        title_html = f"<a href=\"{escape(quota_url)}\">{title}</a>"
    else:
        title_html = title
    provider_badge = "Gemini account" if provider == "gemini" else "Codex account"
    disabled_reason = account.get("disabled_reason")
    subtitle = f"status={'disabled' if account.get('disabled') else 'ready'}"
    if disabled_reason:
        subtitle += f" | reason={disabled_reason}"
    return (
        "<div class='account-card__head'>"
        f"<div><div class='account-card__title'>{title_html}</div>"
        f"<div class='account-card__subtitle'>{escape(subtitle)}</div></div>"
        f"<div class='chip chip--solid'>{escape(provider_badge)}</div>"
        "</div>"
    )


def _render_meta_chips(items: list[tuple[str, Any]]) -> str:
    chips = ["<div class='chips'>"]
    for key, value in items:
        chips.append(f"<div class='chip'>{escape(key)}={escape(_display_value(value))}</div>")
    chips.append("</div>")
    return "".join(chips)


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
