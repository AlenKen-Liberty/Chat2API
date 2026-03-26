"""
Usage logger — records every /v1/chat/completions call as a JSONL line.

Each line contains:
  - timestamp (ISO 8601)
  - caller_ip (client address, useful for Tailscale identification)
  - requested_model (what the caller asked for)
  - actual_model (what was actually used after routing/fallback)
  - provider (which backend served the request)
  - degraded (whether a fallback was used)
  - duration_ms (request processing time)
  - status (success / error)
  - error (error message if any)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_log_path: Path | None = None


def init_usage_log(path: str | Path) -> None:
    """Initialize the usage log file."""
    global _log_path
    _log_path = Path(path)
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Usage log: %s", _log_path)


def log_usage(
    caller_ip: str,
    requested_model: str,
    actual_model: str,
    provider: str,
    degraded: bool,
    duration_ms: float,
    status: str = "success",
    error: str = "",
    stream: bool = False,
) -> None:
    """Append a usage record to the JSONL log file."""
    if _log_path is None:
        return

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ip": caller_ip,
        "req_model": requested_model,
        "model": actual_model,
        "provider": provider,
        "degraded": degraded,
        "stream": stream,
        "ms": round(duration_ms, 1),
        "status": status,
    }
    if error:
        record["error"] = error

    line = json.dumps(record, ensure_ascii=False)
    try:
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        logger.warning("Failed to write usage log: %s", exc)


class UsageTimer:
    """Context manager to track request duration."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.duration_ms = 0.0

    def stop(self) -> float:
        self.duration_ms = (time.monotonic() - self.start) * 1000
        return self.duration_ms
