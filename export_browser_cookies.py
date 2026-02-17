#!/usr/bin/env python3
"""
Export Perplexity cookies from local browser profiles to cookies.json.

This captures HttpOnly auth cookies (which document.cookie cannot access),
so it is suitable for Chat2API.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_cookie_jar(browser: str, domain: str):
    try:
        import browser_cookie3
    except ImportError as exc:
        raise SystemExit(
            "browser-cookie3 is required. Install with: pip install browser-cookie3"
        ) from exc

    loaders = {
        "chrome": browser_cookie3.chrome,
        "chromium": browser_cookie3.chromium,
        "edge": browser_cookie3.edge,
        "firefox": browser_cookie3.firefox,
        "safari": browser_cookie3.safari,
        "brave": browser_cookie3.brave,
        "opera": browser_cookie3.opera,
    }
    if browser not in loaders:
        raise SystemExit(f"Unsupported browser: {browser}")
    return loaders[browser](domain_name=domain)


def _to_chat2api_cookies(cookie_jar):
    out = []
    for c in cookie_jar:
        # Keep a stable shape; Chat2API only needs name/value but extra fields help debugging.
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": bool(c.secure),
                "expires": int(c.expires) if c.expires else 0,
            }
        )
    # Deduplicate by (name, domain, path), keep last one.
    dedup = {}
    for c in out:
        dedup[(c["name"], c["domain"], c["path"])] = c
    return list(dedup.values())


def main():
    parser = argparse.ArgumentParser(
        description="Export Perplexity cookies from browser profile"
    )
    parser.add_argument(
        "--browser",
        default="chrome",
        choices=["chrome", "chromium", "edge", "firefox", "safari", "brave", "opera"],
        help="Browser to read cookies from (default: chrome)",
    )
    parser.add_argument(
        "--domain",
        default="perplexity.ai",
        help="Domain filter (default: perplexity.ai)",
    )
    parser.add_argument(
        "--out",
        default="cookies.json",
        help="Output JSON path (default: cookies.json)",
    )
    args = parser.parse_args()

    jar = _load_cookie_jar(args.browser, args.domain)
    cookies = _to_chat2api_cookies(jar)
    if not cookies:
        raise SystemExit(
            "No cookies found. Make sure you are logged into Perplexity in this browser."
        )

    out_path = Path(args.out).resolve()
    out_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(cookies)} cookies to {out_path}")
    print("You can now start Chat2API and test /v1/chat/completions.")


if __name__ == "__main__":
    main()
