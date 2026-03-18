from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


GEMINI_DIR = Path.home() / ".gemini"
ACCOUNTS_DIR = GEMINI_DIR / "accounts"
ACCOUNTS_INDEX_PATH = ACCOUNTS_DIR / "accounts.json"
GOOGLE_ACCOUNTS_PATH = GEMINI_DIR / "google_accounts.json"
OAUTH_CREDS_PATH = GEMINI_DIR / "oauth_creds.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
USER_AGENT = "GeminiCLI/1.0.0"

# Kept in-process only; phase 1 reads existing CLI credentials and refreshes them.
# The user must provide their own Gemini API keys or public OEM client IDs in .env.
_CLIENT_PAIRS = []
_gemini_client_id = os.getenv("GEMINI_CLIENT_ID")
_gemini_client_secret = os.getenv("GEMINI_CLIENT_SECRET")
if _gemini_client_id and _gemini_client_secret:
    _CLIENT_PAIRS.append((_gemini_client_id, _gemini_client_secret))


class GeminiAuthError(RuntimeError):
    pass


@dataclass
class GeminiToken:
    access_token: str
    refresh_token: str
    expires_in: int
    expiry_timestamp: int
    email: str | None = None
    project_id: str | None = None

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        return time.time() >= (self.expiry_timestamp - buffer_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": self.expires_in,
            "expiry_timestamp": self.expiry_timestamp,
            "email": self.email,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GeminiToken":
        expiry_timestamp = data.get("expiry_timestamp")
        if not expiry_timestamp and data.get("expiry_date"):
            expiry_timestamp = int(int(data["expiry_date"]) / 1000)
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_in=int(data.get("expires_in", 3600)),
            expiry_timestamp=int(expiry_timestamp or 0),
            email=data.get("email"),
            project_id=data.get("project_id"),
        )


@dataclass
class GeminiAccount:
    email: str
    token: GeminiToken
    project_id: str | None = None
    subscription_tier: str | None = None
    quota: dict[str, Any] | None = None
    disabled: bool = False
    disabled_reason: str | None = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_used: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "token": self.token.to_dict(),
            "project_id": self.project_id,
            "subscription_tier": self.subscription_tier,
            "quota": self.quota,
            "disabled": self.disabled,
            "disabled_reason": self.disabled_reason,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GeminiAccount":
        token = GeminiToken.from_dict(data.get("token") or {})
        if data.get("email") and not token.email:
            token.email = data.get("email")
        if data.get("project_id") and not token.project_id:
            token.project_id = data.get("project_id")
        return cls(
            email=data.get("email", token.email or ""),
            token=token,
            project_id=data.get("project_id") or token.project_id,
            subscription_tier=data.get("subscription_tier"),
            quota=data.get("quota"),
            disabled=bool(data.get("disabled", False)),
            disabled_reason=data.get("disabled_reason"),
            created_at=int(data.get("created_at") or int(time.time())),
            last_used=int(data.get("last_used") or int(time.time())),
        )


def _ensure_accounts_dir() -> None:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_accounts_dir()
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)


def _account_path(email: str) -> Path:
    return ACCOUNTS_DIR / f"{email}.json"


def save_account(account: GeminiAccount) -> None:
    _atomic_write_json(_account_path(account.email), account.to_dict())


def load_account(email: str) -> GeminiAccount:
    return GeminiAccount.from_dict(_read_json(_account_path(email)))


def _fallback_accounts() -> list[GeminiAccount]:
    if not GOOGLE_ACCOUNTS_PATH.exists():
        return []

    current = _read_json(GOOGLE_ACCOUNTS_PATH)
    emails = [current.get("active")] + list(current.get("old") or [])
    accounts: list[GeminiAccount] = []

    for email in filter(None, emails):
        candidate = GEMINI_DIR / f"creds_{email}.json"
        if not candidate.exists():
            continue
        raw = _read_json(candidate)
        token = GeminiToken.from_dict(raw)
        token.email = email
        token.project_id = raw.get("project_id")
        accounts.append(
            GeminiAccount(
                email=email,
                token=token,
                project_id=raw.get("project_id"),
            )
        )

    if not accounts and OAUTH_CREDS_PATH.exists():
        raw = _read_json(OAUTH_CREDS_PATH)
        token = GeminiToken.from_dict(raw)
        email = current.get("active") or raw.get("email") or "unknown"
        token.email = email
        accounts.append(GeminiAccount(email=email, token=token))

    return accounts


def list_accounts() -> list[GeminiAccount]:
    if not ACCOUNTS_INDEX_PATH.exists():
        return _fallback_accounts()

    index = _read_json(ACCOUNTS_INDEX_PATH)
    accounts: list[GeminiAccount] = []
    for email in index.get("accounts") or []:
        path = _account_path(email)
        if path.exists():
            accounts.append(load_account(email))
    return accounts


def get_preferred_account() -> GeminiAccount:
    active_email = None
    if ACCOUNTS_INDEX_PATH.exists():
        active_email = (_read_json(ACCOUNTS_INDEX_PATH)).get("active_account")

    accounts = list_accounts()
    enabled = [account for account in accounts if not account.disabled]
    if not enabled:
        raise GeminiAuthError("No enabled Gemini accounts found in ~/.gemini/accounts")

    if active_email:
        for account in enabled:
            if account.email == active_email:
                return account
    return sorted(enabled, key=lambda account: account.last_used)[0]


def _fetch_user_info(access_token: str) -> dict[str, Any]:
    req = urllib.request.Request(USERINFO_URL, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_form(params: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def refresh_token(refresh_token: str) -> GeminiToken:
    last_error: Exception | None = None
    for client_id, client_secret in _CLIENT_PAIRS:
        try:
            payload = _post_form(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
            )
            return GeminiToken(
                access_token=payload["access_token"],
                refresh_token=payload.get("refresh_token") or refresh_token,
                expires_in=int(payload.get("expires_in", 3600)),
                expiry_timestamp=int(time.time()) + int(payload.get("expires_in", 3600)),
            )
        except Exception as exc:  # pragma: no cover - depends on live provider
            last_error = exc
    raise GeminiAuthError(f"Gemini token refresh failed: {last_error}") from last_error


def ensure_fresh_account(account: GeminiAccount, force: bool = False) -> GeminiAccount:
    if not force and not account.token.is_expired():
        return account

    try:
        refreshed = refresh_token(account.token.refresh_token)
    except (urllib.error.HTTPError, urllib.error.URLError, GeminiAuthError) as exc:
        account.disabled = True
        account.disabled_reason = str(exc)
        if _account_path(account.email).exists():
            save_account(account)
        raise GeminiAuthError(f"Failed to refresh Gemini account {account.email}") from exc

    refreshed.email = account.email
    refreshed.project_id = account.project_id or account.token.project_id
    account.token = refreshed
    account.last_used = int(time.time())

    if not account.email:
        info = _fetch_user_info(account.token.access_token)
        account.email = info.get("email", account.email)
        account.token.email = account.email

    if _account_path(account.email).exists():
        save_account(account)
    return account
