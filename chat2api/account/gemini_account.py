from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import urlsafe_b64decode
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

_CLIENT_SECRET_RE = re.compile(r"(GOCSPX-[A-Za-z0-9_-]+)")
_CLIENT_ID_RE = re.compile(r"(\d+-[a-z0-9]+\.apps\.googleusercontent\.com)")


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
    enabled = [account for account in accounts if not account.disabled] or accounts
    if not enabled:
        raise GeminiAuthError("No enabled Gemini accounts found in ~/.gemini/accounts")

    if active_email:
        for account in enabled:
            if account.email == active_email:
                return account
    return sorted(enabled, key=lambda account: account.last_used)[0]


def _decode_jwt_email(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    try:
        payload = token.split(".")[1]
        padding = "=" * (-len(payload) % 4)
        data = json.loads(urlsafe_b64decode(payload + padding).decode("utf-8"))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    email = data.get("email")
    return email if isinstance(email, str) else None


def get_active_account_email() -> str | None:
    if GOOGLE_ACCOUNTS_PATH.exists():
        active = (_read_json(GOOGLE_ACCOUNTS_PATH)).get("active")
        if active:
            return active
    if OAUTH_CREDS_PATH.exists():
        raw = _read_json(OAUTH_CREDS_PATH)
        if raw.get("email"):
            return raw["email"]
        decoded = _decode_jwt_email(raw.get("id_token"))
        if decoded:
            return decoded
    if ACCOUNTS_INDEX_PATH.exists():
        return (_read_json(ACCOUNTS_INDEX_PATH)).get("active_account")
    return None


def _credential_path(email: str) -> Path:
    return GEMINI_DIR / f"creds_{email}.json"


def _list_credential_paths() -> list[Path]:
    return sorted(GEMINI_DIR.glob("creds_*.json"))


def _credential_matches_account(raw: dict[str, Any], account: GeminiAccount) -> bool:
    email = raw.get("email") or _decode_jwt_email(raw.get("id_token"))
    if email == account.email:
        return True
    if raw.get("project_id") and raw.get("project_id") == account.project_id:
        return True
    if raw.get("refresh_token") and raw.get("refresh_token") == account.token.refresh_token:
        return True
    return False


def _upsert_index_account(email: str, *, active_email: str | None = None) -> None:
    index = _read_json(ACCOUNTS_INDEX_PATH) if ACCOUNTS_INDEX_PATH.exists() else {"accounts": []}
    accounts = list(index.get("accounts") or [])
    if email not in accounts:
        accounts.append(email)
    index["accounts"] = accounts
    if active_email is not None:
        index["active_account"] = active_email
    _atomic_write_json(ACCOUNTS_INDEX_PATH, index)


def _write_google_accounts(active_email: str, ordered_emails: list[str]) -> None:
    old = [email for email in ordered_emails if email != active_email]
    _atomic_write_json(
        GOOGLE_ACCOUNTS_PATH,
        {
            "active": active_email,
            "old": old,
        },
    )


def _snapshot_current_oauth_creds() -> None:
    active_email = get_active_account_email()
    if not active_email or not OAUTH_CREDS_PATH.exists():
        return
    raw = _read_json(OAUTH_CREDS_PATH)
    raw["email"] = active_email
    _atomic_write_json(_credential_path(active_email), raw)


def _resolve_credential_blob(account: GeminiAccount) -> dict[str, Any]:
    dedicated = _credential_path(account.email)
    if dedicated.exists():
        return _read_json(dedicated)

    if OAUTH_CREDS_PATH.exists():
        current = _read_json(OAUTH_CREDS_PATH)
        if _credential_matches_account(current, account):
            return current

    for path in _list_credential_paths():
        raw = _read_json(path)
        if _credential_matches_account(raw, account):
            return raw

    current = _read_json(OAUTH_CREDS_PATH) if OAUTH_CREDS_PATH.exists() else {}
    return {
        "access_token": account.token.access_token,
        "refresh_token": account.token.refresh_token,
        "expiry_date": int(account.token.expiry_timestamp) * 1000 if account.token.expiry_timestamp else None,
        "scope": current.get("scope"),
        "token_type": current.get("token_type") or "Bearer",
        "id_token": current.get("id_token") if _decode_jwt_email(current.get("id_token")) == account.email else None,
        "project_id": account.project_id or account.token.project_id,
        "email": account.email,
    }


def set_active_account(email: str) -> GeminiAccount:
    accounts = {account.email: account for account in list_accounts()}
    account = accounts.get(email)
    if account is None:
        raise GeminiAuthError(f"Gemini account '{email}' not found")

    _snapshot_current_oauth_creds()
    blob = _resolve_credential_blob(account)
    blob["email"] = account.email
    blob["project_id"] = account.project_id or account.token.project_id or blob.get("project_id")
    blob["access_token"] = blob.get("access_token") or account.token.access_token
    blob["refresh_token"] = blob.get("refresh_token") or account.token.refresh_token
    if not blob.get("expiry_date") and account.token.expiry_timestamp:
        blob["expiry_date"] = int(account.token.expiry_timestamp) * 1000

    _atomic_write_json(OAUTH_CREDS_PATH, blob)
    _atomic_write_json(_credential_path(account.email), blob)

    ordered_emails = [item.email for item in list_accounts()]
    if account.email not in ordered_emails:
        ordered_emails.append(account.email)
    _write_google_accounts(account.email, ordered_emails)
    _upsert_index_account(account.email, active_email=account.email)
    return account


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
    client_pairs = _client_pairs()
    if not client_pairs:
        raise GeminiAuthError(
            "Gemini token refresh failed: no OAuth client credentials found in env or installed Gemini CLI"
        )

    for client_id, client_secret in client_pairs:
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
        except urllib.error.HTTPError as exc:  # pragma: no cover - depends on live provider
            body = exc.read().decode("utf-8", errors="ignore")[:500]
            last_error = GeminiAuthError(f"HTTP {exc.code} {body}")
        except Exception as exc:  # pragma: no cover - depends on live provider
            last_error = exc
    raise GeminiAuthError(f"Gemini token refresh failed: {last_error}") from last_error


def ensure_fresh_account(account: GeminiAccount, force: bool = False) -> GeminiAccount:
    if account.disabled and not force and not account.token.is_expired():
        account.disabled = False
        account.disabled_reason = None
        if _account_path(account.email).exists():
            save_account(account)
        return account

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
    account.disabled = False
    account.disabled_reason = None
    account.last_used = int(time.time())

    if not account.email:
        info = _fetch_user_info(account.token.access_token)
        account.email = info.get("email", account.email)
        account.token.email = account.email

    if _account_path(account.email).exists():
        save_account(account)
    return account


def _client_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for client_id_key, client_secret_key in [
        ("GEMINI_CLIENT_ID", "GEMINI_CLIENT_SECRET"),
        ("GEMINI_CLI_OAUTH_CLIENT_ID", "GEMINI_CLI_OAUTH_CLIENT_SECRET"),
        ("OPENCLAW_GEMINI_OAUTH_CLIENT_ID", "OPENCLAW_GEMINI_OAUTH_CLIENT_SECRET"),
    ]:
        client_id = os.getenv(client_id_key)
        client_secret = os.getenv(client_secret_key)
        if client_id and client_secret and (client_id, client_secret) not in seen:
            seen.add((client_id, client_secret))
            pairs.append((client_id, client_secret))

    extracted = _extract_gemini_cli_client_pair()
    if extracted and extracted not in seen:
        pairs.append(extracted)

    return pairs


def _extract_gemini_cli_client_pair() -> tuple[str, str] | None:
    for candidate in _gemini_cli_oauth2_candidates():
        if not candidate.exists():
            continue
        content = candidate.read_text(encoding="utf-8", errors="ignore")
        id_match = _CLIENT_ID_RE.search(content)
        secret_match = _CLIENT_SECRET_RE.search(content)
        if id_match and secret_match:
            return id_match.group(1), secret_match.group(1)

    return None


def _gemini_cli_oauth2_candidates() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    for root in _gemini_cli_package_roots():
        for candidate in (
            root / "node_modules" / "@google" / "gemini-cli-core" / "dist" / "src" / "code_assist" / "oauth2.js",
            root / "node_modules" / "@google" / "gemini-cli-core" / "dist" / "code_assist" / "oauth2.js",
        ):
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)

    return candidates


def _gemini_cli_package_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()

    for executable in _candidate_gemini_cli_paths():
        root = _gemini_cli_package_root_from_executable(executable)
        if root is None or root in seen:
            continue
        seen.add(root)
        roots.append(root)

    home = Path.home()
    for root in (
        home / ".npm-global" / "lib" / "node_modules" / "@google" / "gemini-cli",
        home / ".local" / "lib" / "node_modules" / "@google" / "gemini-cli",
        Path("/usr/local/lib/node_modules/@google/gemini-cli"),
        Path("/usr/lib/node_modules/@google/gemini-cli"),
    ):
        if root in seen:
            continue
        seen.add(root)
        roots.append(root)

    return roots


def _candidate_gemini_cli_paths() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    gemini_path = shutil.which("gemini")
    if gemini_path:
        path = Path(gemini_path)
        seen.add(path)
        candidates.append(path)

    home = Path.home()
    for path in (
        home / ".npm-global" / "bin" / "gemini",
        home / ".local" / "bin" / "gemini",
        Path("/usr/local/bin/gemini"),
        Path("/usr/bin/gemini"),
    ):
        if path in seen:
            continue
        seen.add(path)
        candidates.append(path)

    return candidates


def _gemini_cli_package_root_from_executable(path: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None

    for candidate in (resolved, *resolved.parents):
        if candidate.name == "gemini-cli" and candidate.parent.name == "@google":
            return candidate

    return None
