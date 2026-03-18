from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


def iter_sse_data(lines: Iterable[str | bytes]) -> Iterator[str]:
    buffer: list[str] = []

    for raw_line in lines:
        # curl_cffi iter_lines() yields bytes; decode gracefully
        if isinstance(raw_line, (bytes, bytearray)):
            raw_line = raw_line.decode("utf-8", errors="replace")
        line = raw_line.strip("\r")
        if line == "":
            if buffer:
                payload = "\n".join(buffer).strip()
                buffer.clear()
                if payload:
                    yield payload
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())

    if buffer:
        payload = "\n".join(buffer).strip()
        if payload:
            yield payload


def iter_sse_json(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    for payload in iter_sse_data(lines):
        if payload == "[DONE]":
            continue
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            yield decoded


def encode_sse(payload: dict[str, Any] | str) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {body}\n\n"
