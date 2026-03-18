"""
ModelRouter — 将用户请求的模型名称解析为 (provider, model_id)，
并在 provider 耗尽时提供跨 provider 的回落目标。

路由逻辑（实测结论 2026-03-18）：
  - Gemini：7 个模型全部共享同一配额池 → 同 provider 内无需降级
  - Codex：  全部模型共享同一 weekly 配额池 → 同理
  - 有意义的降级只有：provider A 全部账号耗尽 → 切到 provider B 的 best_model
"""
from __future__ import annotations

import difflib
import time
from dataclasses import dataclass

from chat2api.config import Settings, get_settings
from chat2api.models.openai_types import ModelCard


class UnknownModelError(ValueError):
    def __init__(self, model: str, suggestions: list[str]):
        self.model = model
        self.suggestions = suggestions
        super().__init__(self.message)

    @property
    def message(self) -> str:
        if self.suggestions:
            return (
                f"Unknown model '{self.model}'. "
                f"Did you mean '{self.suggestions[0]}'? "
                f"Use 'gemini' or 'codex' for the best available model."
            )
        return (
            f"Unknown model '{self.model}'. "
            f"Use 'gemini' or 'codex' for the best available model."
        )


@dataclass(frozen=True)
class ModelTarget:
    """Resolved routing target for a single request."""
    # What the user requested (for response headers)
    requested_name: str
    # Provider to use ("gemini" | "codex")
    provider: str
    # The actual model_id to send to the provider API
    model_id: str
    # Provider-wide quota group (all models in same provider share this)
    quota_group: str
    # If this provider is exhausted, fall back to this (provider, model_id)
    fallback_provider: str
    fallback_model_id: str


class ModelRouter:
    """
    Resolves user-visible model names to routing targets.

    Two-level lookup:
      1. models dict: "gemini" → {provider: gemini, model_id: gemini-3.1-pro-preview}
      2. providers dict: quota_group, best_model, fallback provider

    Fallback chain:
      requested provider exhausted → fallback provider's best_model
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        # name → (provider, model_id)
        self._by_name: dict[str, tuple[str, str]] = {}
        for name, entry in settings.models.items():
            self._by_name[name] = (entry.provider, entry.model_id)

    def resolve(self, requested: str) -> ModelTarget:
        """
        Resolve a user-facing model name to a ModelTarget.
        Raises UnknownModelError with fuzzy suggestions if not found.
        """
        if requested not in self._by_name:
            suggestions = difflib.get_close_matches(
                requested, self._by_name.keys(), n=3, cutoff=0.55
            )
            raise UnknownModelError(requested, suggestions)

        provider_name, model_id = self._by_name[requested]
        provider_cfg = self._settings.providers[provider_name]

        # Fallback target
        fallback_name = provider_cfg.fallback
        fallback_cfg = self._settings.providers[fallback_name]

        return ModelTarget(
            requested_name=requested,
            provider=provider_name,
            model_id=model_id,
            quota_group=provider_cfg.quota_group,
            fallback_provider=fallback_name,
            fallback_model_id=fallback_cfg.best_model,
        )

    def resolve_fallback(self, target: ModelTarget) -> ModelTarget:
        """
        Build the fallback ModelTarget when target.provider is exhausted.
        The fallback always uses the other provider's best_model.
        """
        fb_provider = target.fallback_provider
        fb_cfg = self._settings.providers[fb_provider]
        fb_fallback_cfg = self._settings.providers[fb_cfg.fallback]

        return ModelTarget(
            requested_name=target.requested_name,
            provider=fb_provider,
            model_id=fb_cfg.best_model,
            quota_group=fb_cfg.quota_group,
            fallback_provider=fb_cfg.fallback,
            fallback_model_id=fb_fallback_cfg.best_model,
        )

    def list_models(self) -> list[tuple[str, str, str]]:
        """Returns list of (name, provider, model_id) for all registered models."""
        return [(name, provider, model_id) for name, (provider, model_id) in self._by_name.items()]

    def to_model_cards(self) -> list[ModelCard]:
        created = int(time.time())
        return [
            ModelCard(
                id=name,
                created=created,
                owned_by=provider,
                root=name,
                parent=None,
            )
            for name, (provider, _) in self._by_name.items()
        ]

    def get(self, name: str) -> ModelTarget | None:
        """Return ModelTarget if name is known, else None."""
        try:
            return self.resolve(name)
        except UnknownModelError:
            return None


_router: ModelRouter | None = None


def get_model_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter(get_settings())
    return _router


# ── Backward-compat alias so existing imports still work ──
def get_tier_catalog() -> ModelRouter:
    return get_model_router()
