from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from curl_cffi import requests


class TLSClient:
    """
    Phase 1 TLS strategy wrapper.

    `node` intentionally maps to native for now until we capture a closer
    fingerprint target; the strategy surface is in place so we can switch
    without changing the provider code.
    """

    _IMPERSONATE_MAP = {
        "native": None,
        "node": None,
        "chrome": "chrome124",
    }

    def __init__(self, strategy: str = "native", impersonate: str | None = None, timeout: int = 120):
        self.strategy = strategy
        self.impersonate = impersonate or self._IMPERSONATE_MAP.get(strategy)
        self.timeout = timeout
        self._requests = None
        self._session = None

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._ensure_session()
        if self.strategy == "sidecar":
            raise NotImplementedError("sidecar TLS transport is not implemented yet")
        kwargs.setdefault("timeout", self.timeout)
        if self.impersonate:
            kwargs.setdefault("impersonate", self.impersonate)
        return self._session.request(method, url, **kwargs)

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[requests.Response]:
        self._ensure_session()
        if self.strategy == "sidecar":
            raise NotImplementedError("sidecar TLS transport is not implemented yet")
        kwargs.setdefault("timeout", self.timeout)
        if self.impersonate:
            kwargs.setdefault("impersonate", self.impersonate)
        with self._session.stream(method, url, **kwargs) as response:
            yield response

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        try:
            from curl_cffi import requests
        except ModuleNotFoundError as exc:
            raise TLSClientDependencyError(
                "curl_cffi is required for provider requests. Install dependencies with "
                "`pip install -r requirements.txt`."
            ) from exc
        self._requests = requests
        self._session = requests.Session()


class TLSClientDependencyError(RuntimeError):
    """Raised when the TLS transport dependency is missing."""
