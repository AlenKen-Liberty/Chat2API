from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7860


class ProviderSettings(BaseModel):
    quota_group: str
    best_model: str
    fallback: str  # provider name to fall back to when exhausted


class ModelEntrySettings(BaseModel):
    provider: Literal["gemini", "codex"]
    model_id: str


class AntiDetectionSettings(BaseModel):
    tls_strategy: str = "native"
    tls_impersonate: str | None = None
    max_rpm_per_account: int = 10
    single_concurrency: bool = True


class QuotaSettings(BaseModel):
    cache_ttl: int = 60
    safety_threshold: int = 40
    check_interval: int = 300
    refresh_retry_interval: int = 600


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    models: dict[str, ModelEntrySettings] = Field(default_factory=dict)
    anti_detection: AntiDetectionSettings = Field(default_factory=AntiDetectionSettings)
    quota: QuotaSettings = Field(default_factory=QuotaSettings)


def _default_config() -> dict:
    return {
        "server": {"host": "127.0.0.1", "port": 7860},
        "providers": {
            "gemini": {
                "quota_group": "gemini-all",
                "best_model": "gemini-3.1-pro-preview",
                "fallback": "codex",
            },
            "codex": {
                "quota_group": "codex-weekly",
                "best_model": "gpt-5.4",
                "fallback": "gemini",
            },
        },
        "models": {
            "gemini": {"provider": "gemini", "model_id": "gemini-3.1-pro-preview"},
            "codex":  {"provider": "codex",  "model_id": "gpt-5.4"},
            "gemini-thinking": {"provider": "gemini", "model_id": "gemini-3.1-pro-preview"},
            "gemini-balanced": {"provider": "gemini", "model_id": "gemini-3-flash-preview"},
            "gemini-fast": {"provider": "gemini", "model_id": "gemini-3.1-flash-lite-preview"},
            "gemini-pro": {"provider": "gemini", "model_id": "gemini-3.1-pro-preview"},
            "gemini-flash": {"provider": "gemini", "model_id": "gemini-3-flash-preview"},
            "gemini-lite": {"provider": "gemini", "model_id": "gemini-3.1-flash-lite-preview"},
            "codex-thinking": {"provider": "codex", "model_id": "gpt-5.4"},
            "codex-balanced": {"provider": "codex", "model_id": "gpt-5.3-codex"},
            "codex-fast": {"provider": "codex", "model_id": "gpt-5.1-codex-mini"},
        },
        "anti_detection": {
            "tls_strategy": "native",
            "tls_impersonate": None,
            "max_rpm_per_account": 10,
            "single_concurrency": True,
        },
        "quota": {
            "cache_ttl": 60,
            "safety_threshold": 40,
            "check_interval": 300,
            "refresh_retry_interval": 600,
        },
    }


def _config_path() -> Path:
    raw = os.getenv("CHAT2API_CONFIG")
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = Path(config_path).expanduser() if config_path else _config_path()
    data = _default_config()

    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        # Deep merge: don't clobber entire sub-dicts
        for key, value in loaded.items():
            if key in data and isinstance(data[key], dict) and isinstance(value, dict):
                data[key].update(value)
            else:
                data[key] = value

    if host := os.getenv("CHAT2API_HOST"):
        data.setdefault("server", {})["host"] = host
    if port := os.getenv("CHAT2API_PORT"):
        data.setdefault("server", {})["port"] = int(port)

    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
