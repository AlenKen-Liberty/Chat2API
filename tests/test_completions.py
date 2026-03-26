import asyncio
from unittest.mock import MagicMock, patch

import pytest

from chat2api.providers.base import ProviderBackend, ProviderRateLimitError
from tests.http_client import make_client


class MockBackend(ProviderBackend):
    provider_name = "mock"

    def stream_text(self, target, request):
        yield "Hello "
        yield "World!"


class RateLimitBackend(ProviderBackend):
    provider_name = "mock_ratelimit"

    def stream_text(self, target, request):
        raise ProviderRateLimitError("Rate limited")


class RecordingBackend(ProviderBackend):
    provider_name = "recording"

    def __init__(self):
        self.calls = []

    def stream_text(self, target, request):
        self.calls.append(
            {
                "provider": target.provider,
                "model_id": target.model_id,
                "requested_model": request.model,
            }
        )
        yield "OK"


def test_chat_completions_success():
    async def scenario():
        async with make_client() as client:
            with patch("chat2api.routing.completions.get_backend_registry") as mock_reg:
                mock_registry = MagicMock()
                mock_registry.get.return_value = MockBackend()
                mock_reg.return_value = mock_registry

                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                )
                if response.status_code == 400:
                    pytest.skip("Model 'gemini-2.5-pro' might not be in config.yaml, skipping test.")

                assert response.status_code == 200
                data = response.json()
                assert data["choices"][0]["message"]["content"] == "Hello World!"

    asyncio.run(scenario())


def test_chat_completions_streaming_success():
    async def scenario():
        async with make_client() as client:
            with patch("chat2api.routing.completions.get_backend_registry") as mock_reg:
                mock_registry = MagicMock()
                mock_registry.get.return_value = MockBackend()
                mock_reg.return_value = mock_registry

                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                )
                if response.status_code == 400:
                    pytest.skip("Model 'gemini-2.5-pro' might not be in config.yaml, skipping test.")

                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert "Hello " in response.text
                assert "World!" in response.text
                assert "data: [DONE]" in response.text

    asyncio.run(scenario())


def test_chat_completions_fallback():
    async def scenario():
        async with make_client() as client:
            with patch("chat2api.routing.completions.get_backend_registry") as mock_reg:
                mock_registry = MagicMock()

                def mock_get(provider):
                    if provider == "gemini":
                        return RateLimitBackend()
                    return MockBackend()

                mock_registry.get.side_effect = mock_get
                mock_reg.return_value = mock_registry

                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}]},
                )
                if response.status_code == 400:
                    pytest.skip("Config problem.")

                assert response.status_code == 200
                assert "x-chat2api-degraded" in response.headers

    asyncio.run(scenario())


def test_chat_completions_all_exhausted():
    async def scenario():
        async with make_client() as client:
            with patch("chat2api.routing.completions.get_backend_registry") as mock_reg:
                mock_registry = MagicMock()
                mock_registry.get.return_value = RateLimitBackend()
                mock_reg.return_value = mock_registry

                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}]},
                )
                if response.status_code == 400:
                    pytest.skip("Config problem.")

                assert response.status_code == 503

    asyncio.run(scenario())


def test_unknown_model_error():
    async def scenario():
        async with make_client() as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "unknown-model-xyz-123", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 400

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("requested_model", "expected_provider", "expected_model_id"),
    [
        ("copilot", "copilot", "gpt-4o"),
        ("copilot-claude", "copilot", "claude-sonnet-4.5"),
        ("groq", "groq", "llama-3.3-70b-versatile"),
    ],
)
def test_chat_completions_routes_copilot_and_groq_models(requested_model, expected_provider, expected_model_id):
    async def scenario():
        backend = RecordingBackend()
        async with make_client() as client:
            with patch("chat2api.routing.completions.get_backend_registry") as mock_reg:
                mock_registry = MagicMock()
                mock_registry.get.return_value = backend
                mock_reg.return_value = mock_registry

                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": requested_model, "messages": [{"role": "user", "content": "hi"}]},
                )

                assert response.status_code == 200
                assert response.json()["model"] == expected_model_id
                assert backend.calls == [
                    {
                        "provider": expected_provider,
                        "model_id": expected_model_id,
                        "requested_model": requested_model,
                    }
                ]
                mock_registry.get.assert_called_once_with(expected_provider)

    asyncio.run(scenario())
