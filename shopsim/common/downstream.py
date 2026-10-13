"""Service-to-service HTTP with a client span, W3C trace propagation and failure mapping."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, Status, StatusCode

from shopsim.common import faults
from shopsim.common.faults import DependencyError, DependencyUnavailable, UpstreamTimeout


class Downstream:
    def __init__(self, ctx, peer: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.ctx = ctx
        self.peer = peer
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        self.host = parsed.hostname or peer
        self.port = parsed.port or 80
        self.client = httpx.AsyncClient(base_url=self.base_url, transport=transport,
                                        timeout=httpx.Timeout(10.0, connect=2.0),
                                        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))

    async def call(self, method: str, path: str, *, route: str | None = None, json: dict | None = None,
                   params: dict | None = None, user_headers: dict | None = None, content: bytes | None = None,
                   raise_5xx: bool = True) -> httpx.Response:
        """``raise_5xx=False`` hands a 5xx back to the caller (a proxy passes it on) instead of
        raising DependencyError; the client span is marked failed either way."""
        ctx = self.ctx
        route = route or path
        with ctx.tracer.start_as_current_span(f"{method} {route}", kind=SpanKind.CLIENT, attributes={
            "http.request.method": method, "http.method": method,
            "url.full": self.base_url + path, "http.url": self.base_url + path,
            "server.address": self.host, "server.port": self.port,
            "net.peer.name": self.host, "net.peer.port": self.port,
            "peer.service": f"shopsim-{self.peer}", "http.route": route,
        }) as span:
            headers = {"x-shopsim-origin": ctx.service, **(user_headers or {})}
            propagate.inject(headers)
            started = time.perf_counter()
            outcome = "ok"
            try:
                await faults.checkpoint(ctx, "downstream")
                resp = await self.client.request(method, path, json=json, params=params, headers=headers,
                                                 content=content)
            except ConnectionResetError as exc:
                outcome = "reset"
                raise DependencyError(f"{self.peer}: connection reset by peer during {method} {route}") from exc
            except httpx.ConnectError as exc:
                outcome = "refused"
                raise DependencyUnavailable(
                    f"{self.peer} unreachable at {self.host}:{self.port}: connection refused") from exc
            except httpx.TimeoutException as exc:
                outcome = "timeout"
                raise UpstreamTimeout(f"{self.peer} did not respond within 10s ({method} {route})") from exc
            finally:
                ctx.metrics.downstream_duration.record((time.perf_counter() - started) * 1000,
                                                       {"peer.service": self.peer, "outcome": outcome})
            span.set_attribute("http.response.status_code", resp.status_code)
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR, f"HTTP {resp.status_code}"))
                if not raise_5xx:
                    return resp
                try:
                    detail = resp.json().get("message", "")
                except ValueError:
                    detail = resp.text[:200]
                raise DependencyError(f"{self.peer} returned {resp.status_code} for {method} {route}: {detail}")
            return resp

    async def aclose(self) -> None:
        await self.client.aclose()


def error_message(resp: httpx.Response) -> str:
    try:
        return resp.json().get("message") or resp.text[:200]
    except ValueError:
        return resp.text[:200]
