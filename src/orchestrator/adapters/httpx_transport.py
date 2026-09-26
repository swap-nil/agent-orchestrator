"""httpx implementation of the HTTP transport port (HTTP/2, pooled, mTLS-capable)."""

from __future__ import annotations

from typing import Any

import ssl

import httpx

from ..transport import HttpResponse, TransportError


class HttpxTransport:
    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | bool = True,
        max_connections: int = 200,
        max_keepalive: int = 50,
    ) -> None:
        self._client = httpx.AsyncClient(
            http2=True,
            verify=ssl_context,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_keepalive),
            follow_redirects=False,
        )

    @staticmethod
    def _wrap(response: httpx.Response) -> HttpResponse:
        body: Any
        if "json" in response.headers.get("content-type", ""):
            try:
                body = response.json()
            except ValueError:
                body = response.text
        else:
            body = response.text
        return HttpResponse(response.status_code, body, dict(response.headers))

    async def post(self, url, *, json_body=None, form=None, headers=None, timeout_s):  # type: ignore[no-untyped-def]
        try:
            response = await self._client.post(url, json=json_body, data=form, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise TransportError(type(exc).__name__) from exc
        return self._wrap(response)

    async def get(self, url, *, headers=None, timeout_s):  # type: ignore[no-untyped-def]
        try:
            response = await self._client.get(url, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise TransportError(type(exc).__name__) from exc
        return self._wrap(response)

    async def aclose(self) -> None:
        await self._client.aclose()
