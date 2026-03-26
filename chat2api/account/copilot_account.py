"""
GitHub Copilot account management.

Auth flow:
  1. A GitHub OAuth token (ghu_*) obtained via device code flow is stored on disk.
  2. That token is exchanged at api.github.com/copilot_internal/v2/token for a
     short-lived Copilot session token (~30 min) with API endpoint info.
  3. The session token is cached in memory and refreshed automatically.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# Where the long-lived GitHub OAuth token lives (compatible with litellm layout)
DEFAULT_TOKEN_DIR = Path.home() / ".config" / "litellm" / "github_copilot"
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
PREMIUM_REQUEST_RESET_NOTE = "Resets on the 1st of each month at 00:00 UTC"
COPILOT_BILLING_URL = "https://api.github.com/user/copilot"


class CopilotAuthError(RuntimeError):
    pass


@dataclass
class CopilotAccount:
    """Represents a single GitHub Copilot-capable GitHub account."""
    github_token: str                          # long-lived ghu_* token
    username: str = ""
    sku: str = ""                              # e.g. "free_educational_quota", "copilot_for_individuals"
    # Short-lived copilot session token (refreshed automatically)
    _session_token: str = field(default="", repr=False)
    _session_expires_at: int = 0
    _api_base: str = "https://api.githubcopilot.com"
    _lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def email(self) -> str:
        return self.username or "github-copilot"

    @property
    def auth_mode(self) -> str:
        return "GitHub OAuth"

    @property
    def plan_name(self) -> str:
        return infer_plan_from_sku(self.sku)["label"]

    @property
    def premium_requests_per_month(self) -> int | None:
        return infer_plan_from_sku(self.sku)["premium_requests_per_month"]

    @property
    def premium_usage(self) -> dict[str, Any] | None:
        """Fetch live premium-request usage from GitHub API.

        Returns dict with keys: usage_percent, used, limit, reset_date
        or None if unavailable.
        """
        return fetch_copilot_premium_usage(self.github_token)

    @property
    def session_token(self) -> str:
        """Return a valid Copilot session token, refreshing if needed."""
        with self._lock:
            if self._session_token and time.time() < self._session_expires_at - 60:
                return self._session_token
            self._refresh_session()
            return self._session_token

    @property
    def api_base(self) -> str:
        """Return the Copilot API base URL (may vary by plan)."""
        # Ensure session is fresh so api_base is populated
        _ = self.session_token
        return self._api_base

    def _refresh_session(self) -> None:
        """Exchange the GitHub token for a short-lived Copilot session token."""
        req = urllib.request.Request(COPILOT_TOKEN_URL)
        req.add_header("Authorization", f"token {self.github_token}")
        req.add_header("User-Agent", "GitHubCopilotChat/0.24.0")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read(500).decode(errors="ignore")
            raise CopilotAuthError(
                f"Failed to get Copilot session token: HTTP {exc.code} {body}"
            ) from exc

        self._session_token = data["token"]
        self._session_expires_at = int(data.get("expires_at", time.time() + 1500))
        endpoints = data.get("endpoints", {})
        self._api_base = endpoints.get("api", "https://api.githubcopilot.com")
        self.sku = data.get("sku", self.sku)
        logger.info(
            "Copilot session refreshed for %s (sku=%s, expires=%s, api=%s)",
            self.username or "unknown", self.sku, self._session_expires_at, self._api_base,
        )


def infer_plan_from_sku(sku: str | None) -> dict[str, Any]:
    normalized = (sku or "").strip().lower().replace("-", "_")

    if "enterprise" in normalized:
        return {"label": "Copilot Enterprise", "premium_requests_per_month": 1000}
    if "business" in normalized:
        return {"label": "Copilot Business", "premium_requests_per_month": 300}
    if "student" in normalized or "educational" in normalized:
        return {"label": "Copilot Student", "premium_requests_per_month": 300}
    if "pro_plus" in normalized or normalized.endswith("_plus") or normalized.endswith("plus"):
        return {"label": "Copilot Pro+", "premium_requests_per_month": 1500}
    if "individual" in normalized or normalized.endswith("_pro") or normalized == "pro":
        return {"label": "Copilot Pro", "premium_requests_per_month": 300}
    if normalized.startswith("free") or normalized == "free":
        return {"label": "Copilot Free", "premium_requests_per_month": 50}

    return {"label": "Copilot plan (unknown SKU)", "premium_requests_per_month": None}


def _read_github_token(token_dir: Path | None = None) -> str:
    """Read the long-lived GitHub OAuth token from disk."""
    d = token_dir or DEFAULT_TOKEN_DIR
    access_token_file = d / "access-token"
    if access_token_file.exists():
        token = access_token_file.read_text().strip()
        if token:
            return token

    # Fallback: try gh CLI config
    gh_hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if gh_hosts.exists():
        import yaml
        with gh_hosts.open() as f:
            hosts = yaml.safe_load(f) or {}
        gh = hosts.get("github.com", {})
        token = gh.get("oauth_token", "")
        if token:
            return token

    raise CopilotAuthError(
        f"No GitHub token found. Expected at {access_token_file} or ~/.config/gh/hosts.yml"
    )


def _resolve_username(token: str) -> str:
    """Fetch GitHub username for display purposes."""
    req = urllib.request.Request("https://api.github.com/user")
    req.add_header("Authorization", f"token {token}")
    req.add_header("User-Agent", "Chat2API/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("login", "")
    except Exception:
        return ""


_account: CopilotAccount | None = None


def get_copilot_account() -> CopilotAccount:
    """Get the singleton CopilotAccount (lazy-initialized)."""
    global _account
    if _account is not None:
        return _account

    token = _read_github_token()
    username = _resolve_username(token)
    _account = CopilotAccount(github_token=token, username=username)
    logger.info("Copilot account loaded: %s", username)
    return _account


def ensure_fresh_account(account: CopilotAccount | None = None) -> CopilotAccount:
    account = account or get_copilot_account()
    _ = account.session_token
    return account


def list_accounts() -> list[CopilotAccount]:
    try:
        return [get_copilot_account()]
    except CopilotAuthError:
        return []


def get_active_account_email() -> str | None:
    try:
        return get_copilot_account().email
    except CopilotAuthError:
        return None


def fetch_copilot_premium_usage(github_token: str) -> dict[str, Any] | None:
    """Fetch premium request usage from GitHub's Copilot internal token endpoint.

    Tries GET https://api.github.com/copilot_internal/v2/token to retrieve premium usage.
    Returns a dict with: usage_percent, limit, reset_date, used — or None if unavailable/unlimited.
    """
    req = urllib.request.Request(COPILOT_TOKEN_URL)
    req.add_header("Authorization", f"token {github_token}")
    req.add_header("User-Agent", "Chat2API/1.0")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.debug("Copilot token fetch for usage failed: %s", exc)
        return None

    if not isinstance(data, dict):
        return None

    # The token response contains 'limited_user_quotas' and 'limited_user_reset_date'
    # 'limited_user_quotas' is often None for users without limits, but may contain
    # a dict when usage is being tracked, e.g. {"chat": {"used": 50, "limit": 300}}
    # or similar structure based on GitHub's API.
    # Since the exact format is undocumented, we look for 'used' and 'limit' keys generically
    # or we handle known structures if they appear.
    
    quotas = data.get("limited_user_quotas")
    reset_date = data.get("limited_user_reset_date")

    if not quotas or not isinstance(quotas, dict):
        # If no explicit quota data is returned, we can't derive a precise live percentage.
        # But we can at least return the reset date if it exists.
        if reset_date:
            return {"reset_date": reset_date}
        return None

    # Try to find 'used' and 'limit' in the quotas dict.
    # Typical structure might be {"chat": {"limit": 300, "used": 8}} or similar
    used = None
    limit = None
    
    # Flatten the dict to look for 'used' and 'limit'
    def find_keys(d, target_key):
        if not isinstance(d, dict):
            return None
        if target_key in d:
            return d[target_key]
        for v in d.values():
            if isinstance(v, dict):
                res = find_keys(v, target_key)
                if res is not None:
                    return res
        return None

    used = find_keys(quotas, "used")
    limit = find_keys(quotas, "limit") or find_keys(quotas, "quota")

    if used is not None and limit:
        try:
            pct = round(float(used) / float(limit) * 100, 1)
            return {
                "usage_percent": pct,
                "used": int(used),
                "limit": int(limit),
                "reset_date": reset_date,
            }
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return {"reset_date": reset_date}

