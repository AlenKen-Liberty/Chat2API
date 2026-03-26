from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CODEX_DIR = Path.home() / ".codex"
ACCOUNTS_DIR = CODEX_DIR / "accounts"
ACCOUNTS_INDEX_PATH = ACCOUNTS_DIR / "accounts.json"
AUTH_PATH = CODEX_DIR / "auth.json"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class CodexAuthError(RuntimeError):
    pass


@dataclass
class AccountInfo:
    email: str | None = None
    account_id: str | None = None
    plan_type: str | None = None


@dataclass
class CodexAccount:
    email: str
    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    plan_type: str
    quota_snapshot: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_used: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "account_id": self.account_id,
            "plan_type": self.plan_type,
            "quota_snapshot": self.quota_snapshot,
            "disabled": self.disabled,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CodexAccount":
        return cls(
            email=data.get("email", ""),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            id_token=data.get("id_token", ""),
            account_id=data.get("account_id", ""),
            plan_type=data.get("plan_type", "unknown"),
            quota_snapshot=data.get("quota_snapshot") or {},
            disabled=bool(data.get("disabled", False)),
            created_at=int(data.get("created_at") or int(time.time())),
            last_used=int(data.get("last_used") or int(time.time())),
        )


def _ensure_accounts_dir() -> None:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)


def _account_path(email: str) -> Path:
    return ACCOUNTS_DIR / f"{email}.json"


def save_account(account: CodexAccount) -> None:
    _atomic_write_json(_account_path(account.email), account.to_dict())


def load_account(email: str) -> CodexAccount:
    return CodexAccount.from_dict(_read_json(_account_path(email)))


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_claims(token: str) -> dict[str, Any]:
    if not token or "." not in token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        return json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}


def extract_account_info(id_token: str | None, access_token: str | None) -> AccountInfo:
    id_claims = decode_jwt_claims(id_token or "")
    access_claims = decode_jwt_claims(access_token or "")
    auth_claims = access_claims.get("https://api.openai.com/auth") or {}

    return AccountInfo(
        email=(
            id_claims.get("email")
            or auth_claims.get("chatgpt_email")
            or access_claims.get("email")
            or auth_claims.get("email")
        ),
        account_id=(
            auth_claims.get("chatgpt_account_id")
            or access_claims.get("chatgpt_account_id")
            or id_claims.get("chatgpt_account_id")
        ),
        plan_type=(
            auth_claims.get("chatgpt_plan_type")
            or access_claims.get("chatgpt_plan_type")
            or id_claims.get("chatgpt_plan_type")
        ),
    )


def is_token_expired(token: str, leeway_seconds: int = 300) -> bool:
    claims = decode_jwt_claims(token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return True
    return time.time() >= (float(exp) - leeway_seconds)


def list_accounts() -> list[CodexAccount]:
    if not ACCOUNTS_INDEX_PATH.exists():
        if not AUTH_PATH.exists():
            return []
        auth_data = _read_json(AUTH_PATH)
        tokens = auth_data.get("tokens") or {}
        info = extract_account_info(tokens.get("id_token"), tokens.get("access_token"))
        return [
            CodexAccount(
                email=info.email or "unknown",
                access_token=tokens.get("access_token", ""),
                refresh_token=tokens.get("refresh_token", ""),
                id_token=tokens.get("id_token", ""),
                account_id=tokens.get("account_id") or info.account_id or "",
                plan_type=info.plan_type or "unknown",
            )
        ]

    index = _read_json(ACCOUNTS_INDEX_PATH)
    accounts: list[CodexAccount] = []
    for email in index.get("accounts") or []:
        path = _account_path(email)
        if path.exists():
            accounts.append(load_account(email))
    return accounts


def get_preferred_account() -> CodexAccount:
    active_email = None
    if ACCOUNTS_INDEX_PATH.exists():
        active_email = (_read_json(ACCOUNTS_INDEX_PATH)).get("active_account")

    enabled = [account for account in list_accounts() if not account.disabled]
    if not enabled:
        raise CodexAuthError("No enabled Codex accounts found in ~/.codex/accounts")

    if active_email:
        for account in enabled:
            if account.email == active_email:
                return account
    return sorted(enabled, key=lambda account: account.last_used)[0]


def get_active_account_email() -> str | None:
    if AUTH_PATH.exists():
        auth_data = _read_json(AUTH_PATH)
        tokens = auth_data.get("tokens") or {}
        info = extract_account_info(tokens.get("id_token"), tokens.get("access_token"))
        if info.email:
            return info.email
    if ACCOUNTS_INDEX_PATH.exists():
        return (_read_json(ACCOUNTS_INDEX_PATH)).get("active_account")
    return None


def _upsert_index_account(email: str, *, active_email: str | None = None) -> None:
    index = _read_json(ACCOUNTS_INDEX_PATH) if ACCOUNTS_INDEX_PATH.exists() else {"accounts": []}
    accounts = list(index.get("accounts") or [])
    if email not in accounts:
        accounts.append(email)
    index["accounts"] = accounts
    if active_email is not None:
        index["active_account"] = active_email
    _atomic_write_json(ACCOUNTS_INDEX_PATH, index)


def _snapshot_current_auth() -> None:
    if not AUTH_PATH.exists():
        return
    auth_data = _read_json(AUTH_PATH)
    tokens = auth_data.get("tokens") or {}
    info = extract_account_info(tokens.get("id_token"), tokens.get("access_token"))
    if not info.email:
        return

    existing = load_account(info.email) if _account_path(info.email).exists() else None
    account = CodexAccount(
        email=info.email,
        access_token=tokens.get("access_token", ""),
        refresh_token=tokens.get("refresh_token", ""),
        id_token=tokens.get("id_token", ""),
        account_id=tokens.get("account_id") or info.account_id or (existing.account_id if existing else ""),
        plan_type=info.plan_type or (existing.plan_type if existing else "unknown"),
        quota_snapshot=existing.quota_snapshot if existing else {},
        disabled=existing.disabled if existing else False,
        created_at=existing.created_at if existing else int(time.time()),
        last_used=int(time.time()),
    )
    save_account(account)
    _upsert_index_account(account.email)


def set_active_account(email: str) -> CodexAccount:
    _snapshot_current_auth()
    accounts = {account.email: account for account in list_accounts()}
    account = accounts.get(email)
    if account is None:
        raise CodexAuthError(f"Codex account '{email}' not found")

    auth_data = _read_json(AUTH_PATH) if AUTH_PATH.exists() else {}
    auth_data["auth_mode"] = auth_data.get("auth_mode") or "chatgpt"
    auth_data["OPENAI_API_KEY"] = auth_data.get("OPENAI_API_KEY")
    auth_data["tokens"] = {
        "id_token": account.id_token,
        "access_token": account.access_token,
        "refresh_token": account.refresh_token,
        "account_id": account.account_id,
    }
    auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    _atomic_write_json(AUTH_PATH, auth_data)
    _upsert_index_account(account.email, active_email=account.email)
    return account


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    ).encode("utf-8")

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - depends on live provider
        body = exc.read().decode("utf-8", errors="ignore")
        raise CodexAuthError(f"Codex token refresh failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - depends on live provider
        raise CodexAuthError(f"Codex token refresh failed: {exc.reason}") from exc


def _try_recover_from_auth_json(account: CodexAccount) -> CodexAccount | None:
    """Check if auth.json has fresher tokens for this account (e.g. after CLI re-login)."""
    if not AUTH_PATH.exists():
        return None
    try:
        auth_data = _read_json(AUTH_PATH)
        tokens = auth_data.get("tokens") or {}
        access_token = tokens.get("access_token", "")
        if not access_token:
            return None
        info = extract_account_info(tokens.get("id_token"), access_token)
        if info.email != account.email:
            return None
        if is_token_expired(access_token):
            return None
        account.access_token = access_token
        account.refresh_token = tokens.get("refresh_token") or account.refresh_token
        account.id_token = tokens.get("id_token") or account.id_token
        account.account_id = info.account_id or account.account_id
        account.plan_type = info.plan_type or account.plan_type
        account.last_used = int(time.time())
        save_account(account)
        return account
    except Exception:
        return None


def ensure_fresh_account(account: CodexAccount, force: bool = False) -> CodexAccount:
    if not account.access_token or not account.refresh_token:
        raise CodexAuthError(f"Codex account {account.email} is missing OAuth tokens")
    if not force and not is_token_expired(account.access_token):
        return account

    try:
        refreshed = refresh_tokens(account.refresh_token)
    except CodexAuthError:
        recovered = _try_recover_from_auth_json(account)
        if recovered is not None:
            return recovered
        raise

    account.access_token = refreshed.get("access_token") or account.access_token
    account.refresh_token = refreshed.get("refresh_token") or account.refresh_token
    account.id_token = refreshed.get("id_token") or account.id_token

    info = extract_account_info(account.id_token, account.access_token)
    account.email = info.email or account.email
    account.account_id = info.account_id or account.account_id
    account.plan_type = info.plan_type or account.plan_type
    account.last_used = int(time.time())

    if _account_path(account.email).exists():
        save_account(account)
    return account
