"""Fault injection.

The middleware decides, per request, whether this request fails and how (``pick_error``). The
failure itself fires later at a ``checkpoint`` in the request's code path (start of the handler,
inside the DB call, inside the gateway call, ...), so it lands on the span that would really fail.
"""
from __future__ import annotations

import asyncio
import contextvars
import random
import sqlite3
import time
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from shopsim.common import bugs
from shopsim.config import ERROR_TYPES, Effective, error_applies


class ShopError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class InternalError(ShopError):
    status, code = 500, "internal_error"


class NotFound(ShopError):
    status, code = 404, "not_found"


class ValidationFailed(ShopError):
    status, code = 422, "validation_failed"


class RateLimited(ShopError):
    status, code = 429, "rate_limited"


class OutOfStock(ShopError):
    status, code = 409, "out_of_stock"


class PaymentDeclined(ShopError):
    status, code = 402, "card_declined"


class FraudSuspected(ShopError):
    status, code = 403, "fraud_suspected"


class UpstreamTimeout(ShopError):
    status, code = 504, "timeout"


class GatewayTimeout(ShopError):
    status, code = 504, "gateway_timeout"


class DependencyError(ShopError):
    status, code = 502, "dependency_error"


class DependencyUnavailable(ShopError):
    status, code = 503, "dependency_unavailable"


class ServiceUnavailable(ShopError):
    status, code = 503, "service_unavailable"


@dataclass
class Injection:
    type: str
    consumed: bool = False


_INJECTION: contextvars.ContextVar[Injection | None] = contextvars.ContextVar("shopsim_injection", default=None)


def set_injection(injection: Injection | None) -> contextvars.Token:
    return _INJECTION.set(injection)


def reset_injection(token: contextvars.Token) -> None:
    _INJECTION.reset(token)


def current_injection() -> Injection | None:
    return _INJECTION.get()


def pick_error(eff: Effective, service: str, method: str, rng: random.Random | None = None) -> str | None:
    rng = rng or random
    rate = eff.error_rate_by_method.get(method, eff.error_rate)
    if rate <= 0 or rng.random() >= rate:
        return None
    if eff.forced_error_type and error_applies(eff.forced_error_type, service, method):
        return eff.forced_error_type
    weights = {t: w for t, w in eff.error_types.items() if w > 0 and error_applies(t, service, method)}
    if not weights:
        return "http_500"
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def jitter_ms(base: float, jitter: float = 0.0) -> float:
    if base <= 0 and jitter <= 0:
        return 0.0
    return max(0.0, base * random.lognormvariate(0, 0.25) + random.uniform(0, jitter))


_VALIDATION = [
    "field 'items[0].qty' must be greater than 0",
    "field 'customer.email' is not a valid email address",
    "field 'price' must be a positive number",
    "unknown field 'discount_code'",
    "field 'currency' must be a 3-letter ISO code",
]
_DECLINES = ["insufficient_funds", "do_not_honor", "expired_card", "incorrect_cvc", "lost_card",
             "card_velocity_exceeded"]


async def checkpoint(ctx, point: str, **info) -> None:
    """Fire the pending injected fault if this is the point where it belongs."""
    inj = _INJECTION.get()
    if inj is None or inj.consumed or ERROR_TYPES[inj.type]["point"] != point:
        return
    inj.consumed = True
    eff = ctx.runtime.eff
    kind = inj.type
    ctx.metrics.injected.add(1, {"error.type": kind})
    if kind == "http_500":
        raise InternalError(random.choice([
            "Unexpected error while processing request",
            "Invariant violated: cart total does not match line items",
            "Worker pool exhausted while handling request",
        ]))
    if kind == "rate_limited":
        raise RateLimited(f"Rate limit exceeded: {random.choice([60, 120, 300])} requests/min per client")
    if kind == "validation":
        raise ValidationFailed(f"Request validation failed: {random.choice(_VALIDATION)}")
    if kind == "not_found":
        raise NotFound(f"{info.get('resource', 'resource')} {info.get('id', random.randint(10_000, 99_999))} not found")
    if kind == "timeout":
        with ctx.tracer.start_as_current_span("session.lookup", kind=SpanKind.CLIENT, attributes={
            "peer.service": "session-store", "server.address": "session-store", "server.port": 6379,
        }):
            await asyncio.sleep(eff.timeout_ms / 1000)
            raise UpstreamTimeout(f"session-store read timed out after {eff.timeout_ms:.0f}ms")
    if kind == "db_lock":
        await asyncio.sleep(random.uniform(0.2, 1.2))
        raise sqlite3.OperationalError("database is locked")
    if kind == "exception":
        with ctx.tracer.start_as_current_span(f"{ctx.service}.business_rules"):
            bugs.run_business_rules(ctx.service)
    if kind == "out_of_stock":
        raise OutOfStock(f"Insufficient stock for SKU {info.get('sku', 'SKU-' + str(random.randint(1000, 9999)))}: "
                         f"requested {random.randint(2, 5)}, available 0")
    if kind == "dependency":
        raise ConnectionResetError(104, "Connection reset by peer")
    if kind == "card_declined":
        raise PaymentDeclined(f"Card declined by issuer: {random.choice(_DECLINES)}")
    if kind == "fraud_suspected":
        raise FraudSuspected(f"Transaction blocked by fraud screen (risk score {random.uniform(0.86, 0.99):.2f})")
    if kind == "gateway_timeout":
        await asyncio.sleep(eff.timeout_ms / 1000)
        raise GatewayTimeout(f"cardnet gateway did not respond within {eff.timeout_ms:.0f}ms")


def classify(exc: BaseException) -> tuple[int, str]:
    if isinstance(exc, ShopError):
        return exc.status, exc.code
    if isinstance(exc, sqlite3.OperationalError):
        return 500, "db_error"
    if isinstance(exc, TimeoutError):
        return 504, "timeout"
    return 500, type(exc).__name__


_SKIP_PATHS = ("/health", "/internal", "/docs", "/openapi.json")


class FaultMiddleware:
    """Pure ASGI middleware, inside the OTel server span: latency, injected faults, error
    responses, access logs and request metrics."""

    def __init__(self, app, ctx):
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(_SKIP_PATHS):
            await self.app(scope, receive, send)
            return
        ctx = self.ctx
        eff = ctx.runtime.eff
        method = scope["method"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client_ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or (scope.get("client") or ("",))[0]
        skip_faults = headers.get("x-shopsim-skip-faults") == "1"
        span = trace.get_current_span()
        span.set_attributes({
            "client.address": client_ip,
            "user_agent.original": headers.get("user-agent", ""),
            "shopsim.origin": headers.get("x-shopsim-origin", "client"),
        })
        if headers.get("x-user-id"):
            span.set_attribute("enduser.id", headers["x-user-id"])
        if headers.get("x-session-id"):
            span.set_attribute("session.id", headers["x-session-id"])

        started = time.perf_counter()
        status = {"code": 500, "sent": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                status["sent"] = True
            await send(message)

        error_type = None
        failure: BaseException | None = None
        token = set_injection(None)
        try:
            if not skip_faults and eff.state != "up":
                await asyncio.sleep(random.uniform(0.005, 0.03))
                raise ServiceUnavailable(f"{ctx.service} is unavailable (state={eff.state})")
            if not skip_faults:
                delay = jitter_ms(eff.latency_ms, eff.jitter_ms)
                if delay:
                    await asyncio.sleep(delay / 1000)
                error_type = pick_error(eff, ctx.service, method)
                if error_type:
                    span.set_attribute("shopsim.injected_fault", error_type)
                    set_injection(Injection(error_type))
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a response + telemetry
            failure = exc
            code, error_code = classify(exc)
            status["code"] = code
            span.set_attribute("error.type", error_code)
            if code >= 500:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
                ctx.logger.error("%s %s failed: %s: %s", method, scope["path"], type(exc).__name__, exc,
                                 exc_info=exc, extra={"fields": {"event": "http.error", "error.code": error_code,
                                                                  "http.request.method": method,
                                                                  "url.path": scope["path"]}})
            else:
                ctx.logger.warning("%s %s rejected: %s", method, scope["path"], exc,
                                   extra={"fields": {"event": "http.rejected", "error.code": error_code,
                                                     "http.request.method": method, "url.path": scope["path"]}})
            if not status["sent"]:
                await _json_response(send, code, {"error": error_code, "message": str(exc),
                                                  "service": ctx.service})
        finally:
            reset_injection(token)

        elapsed = (time.perf_counter() - started) * 1000
        route = getattr(scope.get("route"), "path", None) or scope["path"]
        code = status["code"]
        attrs = {"http.request.method": method, "http.route": route, "http.response.status_code": code}
        ctx.metrics.requests.add(1, attrs)
        ctx.metrics.duration.record(elapsed, attrs)
        if code >= 500 or failure is not None and code >= 400:
            ctx.metrics.errors.add(1, {**attrs, "error.type": classify(failure)[1] if failure else str(code)})
        if eff.request_logs:
            level = 40 if code >= 500 else 30 if code >= 400 else 20
            ctx.logger.log(level, '%s %s %s %.1fms', method, scope["path"], code, elapsed, extra={"fields": {
                "event": "http.access", "http.request.method": method, "url.path": scope["path"],
                "http.route": route, "http.response.status_code": code, "duration_ms": round(elapsed, 2),
                "client.address": client_ip, "enduser.id": headers.get("x-user-id", ""),
                "shopsim.injected_fault": error_type or "",
            }})


async def _json_response(send, status: int, body: dict) -> None:
    import json

    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
    await send({"type": "http.response.body", "body": payload})
