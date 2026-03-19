from chat2api.routing.admin import _group_gemini_models, _render_account_action


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
