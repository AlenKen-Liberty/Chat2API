from pathlib import Path
from unittest.mock import MagicMock

from chat2api.providers.openai_compat import OpenAICompatBackend, describe_api_keys


def test_openai_compat_loads_groq_key_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("chat2api.providers.openai_compat.KEYS_DIR", tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_env_key_1234")

    info = describe_api_keys("groq")
    backend = OpenAICompatBackend("groq", "https://api.groq.com/openai/v1", MagicMock())

    assert info["configured"] is True
    assert info["key_count"] == 1
    assert "GROQ_API_KEY" in info["sources"]
    assert backend._keys == ["gsk_test_env_key_1234"]
