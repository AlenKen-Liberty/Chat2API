import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from chat2api.routing.admin import _group_gemini_models, _render_account_action
from tests.http_client import make_client


def test_group_gemini_models_uses_live_quota_bucket_signature():
    shared_quota = {
        "remaining_fraction": 0.82,
        "remaining_percent": 82.0,
        "reset_time": "2026-03-19T21:12:46Z",
        "reset_in": "4h 10m",
    }
    other_quota = {
        "remaining_fraction": 0.25,
        "remaining_percent": 25.0,
        "reset_time": "2026-03-20T01:12:46Z",
        "reset_in": "8h 10m",
    }
    groups = _group_gemini_models(
        [
            {
                "model_id": "gemini-2.5-flash",
                "quota_group": "gemini-all",
                "quota": shared_quota,
            },
            {
                "model_id": "gemini-3.1-pro-preview",
                "quota_group": "gemini-all",
                "quota": shared_quota,
            },
            {
                "model_id": "gemini-3.1-flash-lite-preview",
                "quota_group": "gemini-all",
                "quota": other_quota,
            },
        ]
    )

    assert len(groups) == 2

    shared_group = next(group for group in groups if len(group["models"]) == 2)
    assert shared_group["label"] == "Shared pool (2 models)"
    assert shared_group["quota"]["remaining_percent"] == 82.0
    assert {model["model_id"] for model in shared_group["models"]} == {
        "gemini-2.5-flash",
        "gemini-3.1-pro-preview",
    }


def test_group_gemini_models_falls_back_to_configured_quota_group():
    groups = _group_gemini_models(
        [
            {"model_id": "gemini-2.5-flash", "quota_group": "gemini-all"},
            {"model_id": "gemini-2.5-pro", "quota_group": "gemini-all"},
        ]
    )

    assert len(groups) == 1
    assert groups[0]["label"] == "Shared pool (2 models)"
    assert {model["model_id"] for model in groups[0]["models"]} == {
        "gemini-2.5-flash",
        "gemini-2.5-pro",
    }


def test_render_account_action_uses_distinct_state_styles():
    active_html = _render_account_action(
        {"is_active": True, "activate_url": "/admin/activate-account"},
        provider="gemini",
    )
    inactive_html = _render_account_action(
        {"is_active": False, "activate_url": "/admin/activate-account"},
        provider="gemini",
    )

    assert "chip--active" in active_html
    assert "Active account" in active_html
    assert "chip--action" in inactive_html
    assert "Become active account" in inactive_html


def test_admin_dashboard_lists_copilot_and_groq_tabs():
    async def scenario():
        async with make_client() as client:
            response = await client.get("/admin/quota-urls", headers={"accept": "text/html"})
            assert response.status_code == 200
            assert "GitHub Copilot" in response.text
            assert ">Groq<" in response.text

    asyncio.run(scenario())


def test_admin_copilot_tab_shows_oauth_and_premium_limits():
    async def scenario():
        import time
        account = SimpleNamespace(
            username="octocat",
            email="octocat",
            plan_name="Copilot Pro",
            sku="copilot_for_individuals",
            api_base="https://api.individual.githubcopilot.com",
            _api_base="https://api.individual.githubcopilot.com",
            auth_mode="GitHub OAuth",
            premium_requests_per_month=300,
            premium_usage={"usage_percent": 2.7, "used": 8, "limit": 300, "reset_date": None},
            _premium_usage={"usage_percent": 2.7, "used": 8, "limit": 300, "reset_date": None},
            _session_expires_at=time.time() + 3000,
        )
        async with make_client() as client:
            with patch("chat2api.routing.admin.get_copilot_account", return_value=account), patch(
                "chat2api.routing.admin.ensure_fresh_copilot_account", return_value=account
            ), patch(
                "chat2api.routing.admin.get_active_copilot_account_email", return_value="octocat"
            ):
                response = await client.get("/admin/quota-urls?provider=copilot&format=html")
                assert response.status_code == 200
                assert "GitHub OAuth" in response.text
                assert "2.7%" in response.text
                assert "8 / 300 premium requests used" in response.text
                assert "gpt-4o" in response.text
                assert "claude-opus-4.5 (3x)" in response.text

    asyncio.run(scenario())


def test_admin_copilot_tab_falls_back_to_entitlement_when_no_usage():
    """When premium_usage is None (API unavailable), show N/month."""
    async def scenario():
        import time
        account = SimpleNamespace(
            username="octocat",
            email="octocat",
            plan_name="Copilot Pro",
            sku="copilot_for_individuals",
            api_base="https://api.individual.githubcopilot.com",
            _api_base="https://api.individual.githubcopilot.com",
            auth_mode="GitHub OAuth",
            premium_requests_per_month=300,
            premium_usage=None,
            _premium_usage=None,
            _session_expires_at=time.time() + 3000,
        )
        async with make_client() as client:
            with patch("chat2api.routing.admin.get_copilot_account", return_value=account), patch(
                "chat2api.routing.admin.ensure_fresh_copilot_account", return_value=account
            ), patch(
                "chat2api.routing.admin.get_active_copilot_account_email", return_value="octocat"
            ):
                response = await client.get("/admin/quota-urls?provider=copilot&format=html")
                assert response.status_code == 200
                assert "300/month" in response.text

    asyncio.run(scenario())


def test_admin_groq_tab_shows_env_backed_key_status():
    async def scenario():
        async with make_client() as client:
            with patch(
                "chat2api.routing.admin.describe_api_keys",
                return_value={
                    "provider": "groq",
                    "configured": True,
                    "key_count": 1,
                    "sources": ["GROQ_API_KEY"],
                    "masked_keys": ["gsk_...1234"],
                },
            ):
                response = await client.get("/admin/quota-urls?provider=groq&format=html")
                assert response.status_code == 200
                assert "Groq API keys" in response.text
                assert "GROQ_API_KEY" in response.text
                assert "llama-3.3-70b-versatile" in response.text

    asyncio.run(scenario())
