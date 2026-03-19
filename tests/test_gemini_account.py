from pathlib import Path

from chat2api.account import gemini_account


def test_extract_gemini_cli_client_pair_falls_back_to_home_npm_global(monkeypatch, tmp_path):
    package_root = tmp_path / ".npm-global" / "lib" / "node_modules" / "@google" / "gemini-cli"
    oauth2_js = (
        package_root
        / "node_modules"
        / "@google"
        / "gemini-cli-core"
        / "dist"
        / "src"
        / "code_assist"
        / "oauth2.js"
    )
    oauth2_js.parent.mkdir(parents=True, exist_ok=True)
    oauth2_js.write_text(
        """
        export const CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com";
        export const CLIENT_SECRET = "GOCSPX-test-secret_123";
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr(gemini_account.shutil, "which", lambda _: None)
    monkeypatch.setattr(gemini_account.Path, "home", classmethod(lambda cls: Path(tmp_path)))

    assert gemini_account._extract_gemini_cli_client_pair() == (
        "1234567890-abcdef.apps.googleusercontent.com",
        "GOCSPX-test-secret_123",
    )
