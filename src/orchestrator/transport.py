"""HTTP transport port.

Core clients (A2A, OPA, token exchange) talk HTTP through this small protocol
so they can be unit-tested without a network and without binding the core to
one HTTP library. The production implementation is
``adapters/httpx_transport.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


class TransportError(Exception):
    """Network-level failure (connection refused, TLS error, timeout)."""


@dataclass
class HttpResponse:
    status: int
    body: Any = None  # parsed JSON when the response is JSON, else text
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    async def post(
        self,
        url: str,
        *,
        json_body: Any = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float,
    ) -> HttpResponse: ...

    async def get(self, url: str, *, headers: dict[str, str] | None = None, timeout_s: float) -> HttpResponse: ...


Handler = Callable[[str, str, dict[str, Any]], Awaitable[HttpResponse] | HttpResponse]


class FakeTransport:
    """In-memory transport for tests and local demos.

    ``handler(method, url, request)`` receives the request as a dict with keys
    ``json``, ``form`` and ``headers`` and returns an :class:`HttpResponse`.
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def _call(self, method: str, url: str, request: dict[str, Any]) -> HttpResponse:
        self.requests.append((method, url, request))
        result = self._handler(method, url, request)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        return result  # type: ignore[return-value]

    async def post(self, url, *, json_body=None, form=None, headers=None, timeout_s):  # type: ignore[no-untyped-def]
        return await self._call("POST", url, {"json": json_body, "form": form, "headers": headers or {}, "timeout_s": timeout_s})

    async def get(self, url, *, headers=None, timeout_s):  # type: ignore[no-untyped-def]
        return await self._call("GET", url, {"headers": headers or {}, "timeout_s": timeout_s})
