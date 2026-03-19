from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


RETRIEVE_USER_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
LOAD_CODE_ASSIST_URL = "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class QuotaFetchError(RuntimeError):
    pass


def fetch_gemini_project_info(access_token: str) -> tuple[str | None, str | None]:
    payload = json.dumps(
        {
            "metadata": {
                "ideType": "IDE_UNSPECIFIED",
                "pluginType": "GEMINI",
                "platform": "PLATFORM_UNSPECIFIED",
            }
        }
    ).encode("utf-8")
    req = urllib.request.Request(LOAD_CODE_ASSIST_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "GeminiCLI/1.0.0")

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - depends on live provider
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise QuotaFetchError(f"Gemini loadCodeAssist failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - depends on live provider
        raise QuotaFetchError(f"Gemini loadCodeAssist failed: {exc.reason}") from exc

    project_id = data.get("cloudaicompanionProject")
    tier = None

    paid = data.get("paidTier")
    if paid:
        tier = paid.get("name") or paid.get("id")

    if not tier:
        current = data.get("currentTier")
        if current:
            tier = current.get("name") or current.get("id")

    return project_id, tier


def fetch_gemini_quota(access_token: str, project_id: str | None = None) -> dict[str, Any]:
    payload = json.dumps({"project": project_id} if project_id else {}).encode("utf-8")
    req = urllib.request.Request(RETRIEVE_USER_QUOTA_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "GeminiCLI/1.0.0")

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - depends on live provider
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise QuotaFetchError(f"Gemini retrieveUserQuota failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - depends on live provider
        raise QuotaFetchError(f"Gemini retrieveUserQuota failed: {exc.reason}") from exc


def fetch_codex_usage(access_token: str, account_id: str | None) -> dict[str, Any]:
    req = urllib.request.Request(CODEX_USAGE_URL, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    if account_id:
        req.add_header("ChatGPT-Account-Id", account_id)
    req.add_header("User-Agent", "CodexBar")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - depends on live provider
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise QuotaFetchError(f"Codex wham/usage failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - depends on live provider
        raise QuotaFetchError(f"Codex wham/usage failed: {exc.reason}") from exc


def format_iso_reset_time(value: str | None) -> str | None:
    if not value:
        return None

    try:
        reset_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value

    delta_seconds = max(0, int((reset_dt - datetime.now(timezone.utc)).total_seconds()))
    return _format_duration(delta_seconds)


def format_unix_reset_time(value: int | float | None) -> str | None:
    if not value:
        return None

    delta_seconds = max(0, int(float(value) - time.time()))
    return _format_duration(delta_seconds)


def unix_reset_time_to_iso(value: int | float | None) -> str | None:
    if not value:
        return None

    try:
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat().replace("+00:00", "Z")


def percent(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) * 100, 2)


def remaining_percent_from_used(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(max(0.0, 100.0 - float(value)), 2)


def _format_duration(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "0m"

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
