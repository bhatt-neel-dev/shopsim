"""The storefront: the shop's web pages and the API behind them (a backend-for-frontend).

Browsers (the virtual shoppers, or you) load a small single-page shop that runs the Motadata RUM
SDK. The SDK adds a W3C ``traceparent`` to every call to ``/api/...``, so a click in the browser,
this service's span and the catalog/orders/payments spans behind it are one trace, and Motadata
links the RUM resource to that trace.

This service also relays the SDK's beacons to Motadata's RUM intake (same origin: no CORS and no
self-signed certificate in the browser), and finishes the views the SDK leaves open (see
``RumRelay``).
"""
from __future__ import annotations

import asyncio
import copy
import json
import random
import re
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from opentelemetry import trace
from pydantic import BaseModel, Field

from shopsim import settings as settings_mod
from shopsim.common.downstream import error_message
from shopsim.common.fakedata import now_iso
from shopsim.common.faults import DependencyError, NotFound, ValidationFailed, checkpoint
from shopsim.settings import VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS carts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cart_id TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    sku TEXT, name TEXT, category TEXT,
    price REAL NOT NULL,
    qty INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    UNIQUE (cart_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_carts_added ON carts (added_at);
"""
PEERS = ("catalog", "orders", "payments")
# Faults, access logs and request metrics only for the API; pages, assets and beacons are served
# as-is (a 500 on app.js would just break every page). Only the API is traced.
FAULT_PATHS = ("/api/",)
EXCLUDED_URLS = r"^https?://[^/]+/(?!api/)"

STATIC = Path(__file__).resolve().parent.parent / "storefront"
_SPA_ROUTES = re.compile(r"^/($|category/|product/|search$|cart$|checkout$|orders(/\d+)?$|account$|login$)")
_ID_SEGMENT = re.compile(r"/\d+(?=/|$)")
_CART_COOKIE = "shopsim_cart"
_PALETTE = ["#4f9cf9", "#3fb950", "#d29922", "#a371f7", "#f85149", "#1f9d8f", "#e8590c", "#5c7cfa"]


def seed(ctx) -> None:
    pass


def setup(ctx):
    ctx.state["relay"] = RumRelay(ctx)
    ctx.state["checkouts"] = ctx.meter.create_counter("shopsim.web.checkouts", unit="{checkout}",
                                                      description="Storefront checkouts by outcome")
    ctx.state["cart_adds"] = ctx.meter.create_counter("shopsim.web.cart.adds", unit="{item}",
                                                      description="Items added to carts")

    async def cart_cleanup():
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 6 * 3600))
        res = await ctx.db.query("DELETE FROM carts WHERE added_at < ?", (cutoff,), table="carts")
        pruned = ctx.db.prune("carts", ctx.runtime.eff.max_rows)
        if res.rowcount or pruned:
            ctx.logger.info("Purged %d abandoned cart line(s)", res.rowcount + pruned,
                            extra={"fields": {"event": "cart.cleanup", "count": res.rowcount + pruned}})

    async def warm_cache():
        catalog = ctx.downstream["catalog"]
        resp = await catalog.call("GET", "/categories", route="/categories", raise_5xx=False)
        ctx.logger.debug("Warmed navigation cache (%s)", resp.status_code,
                         extra={"fields": {"event": "cache.warm", "status": resp.status_code}})

    return [("cart.cleanup", 120, cart_cleanup), ("nav.cache.warm", 45, warm_cache)]


def tasks(ctx):
    return [ctx.state["relay"].promote_forever]


def health(ctx) -> dict:
    relay: RumRelay = ctx.state["relay"]
    return {"rum": relay.snapshot()}


# ---- RUM relay ----------------------------------------------------------------------------------
class RumRelay:
    """Forwards SDK beacons to the Motadata RUM intake and completes views left open.

    Motadata stores a ``view`` only once it arrives with ``view.is_view_completed = "yes"``. The
    SDK sends that one minute after the view ends, from inside the page, so a visitor who leaves
    or closes the tab before then never sends it and the view is lost. The relay keeps the latest
    update of each open view; once a view has been quiet for ``idle_s`` it sends that update
    again marked complete. The backend still does all the processing (Apdex, sessions).
    """

    def __init__(self, ctx, idle_s: float = 45, max_pending: int = 5000):
        s = settings_mod.settings
        self.ctx = ctx
        self.intake = s.rum_intake_url.rstrip("/")
        self.idle_s = idle_s
        self.max_pending = max_pending
        self.client = httpx.AsyncClient(verify=s.rum_verify_tls, timeout=httpx.Timeout(10.0, connect=3.0))
        self.pending: OrderedDict[str, dict] = OrderedDict()
        self.recent: deque[dict] = deque(maxlen=200)
        self.stats = {"beacons": 0, "events": 0, "forward_errors": 0, "rejected": 0, "views_completed_by_sdk": 0,
                      "views_completed_by_relay": 0, "last_error": "", "last_status": None, "last_beacon_at": None}
        self.event_counts: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.intake)

    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "intake": self.intake, "pending_views": len(self.pending),
                "events_by_type": dict(self.event_counts), **self.stats}

    async def forward(self, forward_path: str, body: bytes, headers: dict) -> tuple[int, bytes, str]:
        try:
            resp = await self.client.post(self.intake + forward_path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            self.stats["forward_errors"] += 1
            self.stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            return 502, b'{"error":"rum intake unreachable"}', "application/json"
        self.stats["last_status"] = resp.status_code
        if resp.status_code >= 300:
            self.stats["rejected"] += 1
            self.stats["last_error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
        return resp.status_code, resp.content, resp.headers.get("content-type", "application/json")

    def track(self, raw: bytes, forward_path: str, headers: dict) -> None:
        now = time.time()
        self.stats["beacons"] += 1
        self.stats["last_beacon_at"] = now
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type", "?"))
            self.stats["events"] += 1
            self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
            view = event.get("view") if kind == "view" else None
            if not isinstance(view, dict) or not view.get("id"):
                continue
            self._remember(event, view, now)
            if str(view.get("is_view_completed", "no")).lower() == "yes":
                if self.pending.pop(view["id"], None) is not None:
                    self.stats["views_completed_by_sdk"] += 1
                continue
            if view["id"] not in self.pending and len(self.pending) >= self.max_pending:
                self.pending.popitem(last=False)
            self.pending[view["id"]] = {"event": event, "path": forward_path, "headers": headers, "seen": now}
            self.pending.move_to_end(view["id"])

    def _remember(self, event: dict, view: dict, now: float) -> None:
        session = (event.get("session") or {}).get("id", "")
        row = {"time": now_iso(), "session": session[:8], "view": view.get("url_path") or view.get("name") or "",
               "loading_ms": round((view.get("loading_time") or 0) / 1e6, 1),
               "errors": (view.get("error") or {}).get("count", 0),
               "actions": (view.get("action") or {}).get("count", 0),
               "resources": (view.get("resource") or {}).get("count", 0),
               "completed": str(view.get("is_view_completed", "no")), "id": view["id"]}
        for existing in self.recent:
            if existing["id"] == view["id"]:
                existing.update(row)
                return
        self.recent.appendleft(row)

    async def promote_once(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        due = [vid for vid, entry in self.pending.items() if now - entry["seen"] >= self.idle_s]
        done = 0
        for vid in due:
            entry = self.pending.pop(vid)
            event = copy.deepcopy(entry["event"])
            event["view"]["is_view_completed"] = "yes"
            event["view"]["is_active"] = False
            meta = event.get("_md")
            if isinstance(meta, dict) and isinstance(meta.get("document_version"), int):
                meta["document_version"] += 1
            status, _, _ = await self.forward(entry["path"], json.dumps(event).encode(), entry["headers"])
            if status < 300:
                done += 1
                self.stats["views_completed_by_relay"] += 1
                for row in self.recent:
                    if row["id"] == vid:
                        row["completed"] = "relay"
        return done

    async def promote_forever(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await self.promote_once()
            except Exception as exc:  # noqa: BLE001 - never let the sweeper die
                self.stats["last_error"] = f"promote: {type(exc).__name__}: {exc}"[:300]

    async def aclose(self) -> None:
        await self.client.aclose()


# ---- API models ---------------------------------------------------------------------------------
class CartItemIn(BaseModel):
    product_id: int = Field(ge=1)
    qty: int = Field(1, ge=1, le=20)


class CustomerIn(BaseModel):
    id: str = Field(min_length=3, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    tier: str = "standard"
    city: str = ""
    country: str = "US"


class CheckoutIn(BaseModel):
    customer: CustomerIn
    payment_method: str = "card"
    card: dict | None = None
    shipping_method: str = "standard"
    coupon: str | None = Field(None, max_length=30)


def _cart_id(request: Request) -> str:
    return (request.headers.get("x-cart-id") or request.cookies.get(_CART_COOKIE) or "").strip()[:40]


def _with_cart_cookie(resp: Response, cart_id: str) -> Response:
    resp.set_cookie(_CART_COOKIE, cart_id, max_age=7 * 86400, samesite="lax", httponly=False)
    return resp


def _route_of(path: str) -> str:
    return _ID_SEGMENT.sub("/{id}", path)


def product_svg(name: str, delay_label: str = "") -> str:
    seed = sum(map(ord, name))
    color = _PALETTE[seed % len(_PALETTE)]
    label = re.sub(r"[^A-Za-z0-9 #-]", "", name)[:28] or "ShopSim"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480" viewBox="0 0 640 480">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{color}"/>'
            f'<stop offset="1" stop-color="#0e1116"/></linearGradient></defs>'
            f'<rect width="640" height="480" fill="url(#g)"/>'
            f'<circle cx="{120 + seed % 400}" cy="{90 + seed % 260}" r="{60 + seed % 90}" fill="#fff" opacity=".12"/>'
            f'<text x="32" y="440" font-family="system-ui,sans-serif" font-size="34" fill="#fff" '
            f'opacity=".92">{label}</text>{delay_label}</svg>')


# ---- routes -------------------------------------------------------------------------------------
def router(ctx) -> APIRouter:
    r = APIRouter()
    db = ctx.db
    relay: RumRelay = ctx.state["relay"]
    s = settings_mod.settings

    async def api_delay() -> None:
        fe = ctx.runtime.eff.frontend
        if fe is not None and fe.api_latency_ms > 0:
            await asyncio.sleep(fe.api_latency_ms * random.uniform(0.7, 1.3) / 1000)

    async def load_cart(cart_id: str) -> dict:
        rows = (await db.query("SELECT product_id, sku, name, category, price, qty FROM carts WHERE cart_id = ? "
                               "ORDER BY id", (cart_id,), table="carts")).rows if cart_id else []
        total = round(sum(row["price"] * row["qty"] for row in rows), 2)
        return {"cart_id": cart_id, "items": rows, "count": sum(row["qty"] for row in rows), "total": total}

    # -- cart & checkout -------------------------------------------------------------------------
    @r.get("/api/cart")
    async def get_cart(request: Request):
        await api_delay()
        await checkpoint(ctx, "start", resource="cart")
        cart_id = _cart_id(request)
        trace.get_current_span().set_attribute("cart.id", cart_id)
        cart = await load_cart(cart_id)
        await checkpoint(ctx, "logic")
        return cart

    @r.post("/api/cart/items")
    async def add_item(body: CartItemIn, request: Request):
        await api_delay()
        await checkpoint(ctx, "start", resource="cart")
        cart_id = _cart_id(request) or f"CART-{uuid.uuid4().hex[:12]}"
        span = trace.get_current_span()
        span.set_attributes({"cart.id": cart_id, "product.id": body.product_id, "cart.qty": body.qty})
        resp = await ctx.downstream["catalog"].call("GET", f"/products/{body.product_id}",
                                                    route="/products/{product_id}")
        if resp.status_code == 404:
            raise NotFound(f"product {body.product_id} not found")
        if resp.status_code >= 400:
            raise DependencyError(f"catalog answered {resp.status_code}: {error_message(resp)}")
        product = resp.json()
        if product.get("stock", 0) <= 0:
            raise ValidationFailed(f"{product.get('sku')} is out of stock")
        await db.query(
            "INSERT INTO carts (cart_id, product_id, sku, name, category, price, qty, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (cart_id, product_id) DO UPDATE SET qty = MIN(20, qty + "
            "excluded.qty), price = excluded.price, added_at = excluded.added_at",
            (cart_id, body.product_id, product.get("sku"), product.get("name"), product.get("category"),
             product.get("price", 0), body.qty, now_iso()), table="carts")
        await checkpoint(ctx, "logic")
        ctx.state["cart_adds"].add(body.qty, {"category": product.get("category", "")})
        ctx.logger.info("Added %d x %s to cart %s", body.qty, product.get("sku"), cart_id,
                        extra={"fields": {"event": "cart.add", "cart.id": cart_id, "product.id": body.product_id}})
        return _with_cart_cookie(JSONResponse(await load_cart(cart_id), status_code=201), cart_id)

    @r.delete("/api/cart/items/{product_id}")
    async def remove_item(product_id: int, request: Request):
        await api_delay()
        await checkpoint(ctx, "start", resource="cart", id=product_id)
        cart_id = _cart_id(request)
        await db.query("DELETE FROM carts WHERE cart_id = ? AND product_id = ?", (cart_id, product_id),
                       table="carts")
        await checkpoint(ctx, "logic")
        return await load_cart(cart_id)

    @r.post("/api/checkout")
    async def checkout(body: CheckoutIn, request: Request):
        await api_delay()
        await checkpoint(ctx, "start", resource="checkout")
        cart_id = _cart_id(request)
        cart = await load_cart(cart_id)
        span = trace.get_current_span()
        span.set_attributes({"cart.id": cart_id, "cart.items": len(cart["items"]), "cart.total": cart["total"],
                             "enduser.id": body.customer.id})
        if not cart["items"]:
            raise ValidationFailed("cart is empty")
        order = {"customer": body.customer.model_dump(), "payment_method": body.payment_method, "card": body.card,
                 "shipping_method": body.shipping_method, "coupon": body.coupon, "currency": "USD",
                 "items": [{"product_id": i["product_id"], "qty": i["qty"]} for i in cart["items"]]}
        resp = await ctx.downstream["orders"].call("POST", "/orders", route="/orders", json=order,
                                                   user_headers={"x-user-id": body.customer.id}, raise_5xx=False)
        outcome = "placed" if resp.status_code == 201 else f"failed_{resp.status_code}"
        ctx.state["checkouts"].add(1, {"outcome": outcome})
        if resp.status_code == 201:
            await db.query("DELETE FROM carts WHERE cart_id = ?", (cart_id,), table="carts")
            placed = resp.json()
            ctx.logger.info("Checkout complete: %s for %s, %.2f USD", placed.get("order_number"), body.customer.id,
                            placed.get("total", 0), extra={"fields": {
                                "event": "checkout.completed", "order.number": placed.get("order_number"),
                                "cart.id": cart_id, "customer.id": body.customer.id}})
        else:
            ctx.logger.warning("Checkout failed for cart %s: orders answered %d: %s", cart_id, resp.status_code,
                               error_message(resp), extra={"fields": {
                                   "event": "checkout.failed", "cart.id": cart_id, "status": resp.status_code}})
        await checkpoint(ctx, "logic")
        return Response(resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    # -- pass-through to the shop's services (what the pages and API clients read) --------------
    @r.api_route("/api/{peer}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def passthrough(peer: str, path: str, request: Request):
        if peer not in PEERS:
            raise NotFound(f"no API {peer!r}")
        await api_delay()
        await checkpoint(ctx, "start", resource=peer)
        target = "/" + path
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k in ("content-type", "x-user-id", "x-session-id")}
        resp = await ctx.downstream[peer].call(request.method, target, route=_route_of(target),
                                               params=dict(request.query_params), content=body or None,
                                               user_headers=headers, raise_5xx=False)
        await checkpoint(ctx, "logic")
        return Response(resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    # -- RUM beacons -------------------------------------------------------------------------------
    @r.post("/rum", include_in_schema=False)
    async def rum(request: Request, mdforward: str = ""):
        if not relay.enabled:
            return JSONResponse({"error": "RUM intake not configured (RUM_INTAKE_URL)"}, status_code=503)
        if not mdforward.startswith("/api/v2/"):
            return JSONResponse({"error": "bad mdforward"}, status_code=400)
        body = await request.body()
        client_ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else ""))
        # Canonical header names, as a browser or nginx would send them.
        headers = {"Content-Type": request.headers.get("content-type", "text/plain;charset=UTF-8"),
                   "User-Agent": request.headers.get("user-agent", ""), "X-Forwarded-For": client_ip,
                   "X-Real-IP": client_ip.split(",")[0].strip()}
        if request.headers.get("content-encoding"):
            headers["Content-Encoding"] = request.headers["content-encoding"]
        else:
            relay.track(body, mdforward, headers)
        status, content, ctype = await relay.forward(mdforward, body, headers)
        return Response(content, status_code=status, media_type=ctype)

    @r.get("/internal/frontend", include_in_schema=False)
    async def frontend():
        fe = ctx.runtime.eff.frontend
        return JSONResponse(fe.model_dump(mode="json") if fe else {}, headers={"Cache-Control": "no-store"})

    @r.get("/internal/ids", include_in_schema=False)
    async def ids(limit: int = 500):
        return []

    @r.get("/internal/recent", include_in_schema=False)
    async def recent(limit: int = 15):
        return list(relay.recent)[:limit]

    # -- assets ------------------------------------------------------------------------------------
    @r.get("/img/{name}", include_in_schema=False)
    async def image(name: str, delay: float = 0):
        if name.startswith("missing-"):
            return Response("not found", status_code=404, media_type="text/plain")
        if delay > 0:
            await asyncio.sleep(min(delay, 20000) / 1000)
        label = re.sub(r"^p-\d+-", "", name.rsplit(".", 1)[0]).replace("-", " ").title()
        return Response(product_svg(label), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store" if delay else "public, max-age=300"})

    @r.get("/static/{path:path}", include_in_schema=False)
    async def static(path: str):
        target = (STATIC / path).resolve()
        if STATIC not in target.parents or not target.is_file():
            return Response("not found", status_code=404, media_type="text/plain")
        return FileResponse(target, headers={"Cache-Control": "public, max-age=60"})

    @r.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    page_config = {
        "version": VERSION, "env": s.environment,
        "rum": {"applicationId": s.rum_application_id, "clientToken": s.rum_client_token,
                "service": s.rum_service, "env": s.environment, "version": s.rum_version}
        if relay.enabled and s.rum_application_id and s.rum_client_token else None,
    }

    @r.get("/{path:path}", include_in_schema=False)
    async def page(path: str):
        if not _SPA_ROUTES.match("/" + path):
            return HTMLResponse("<h1>404</h1><p>No such page.</p>", status_code=404)
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace("__SHOPSIM_CONFIG__", json.dumps(page_config).replace("</", "<\\/"))
        html = html.replace("__RUM_TAG__", '<script src="/static/vendor/motadata-rum.js"></script>'
                            if page_config["rum"] else "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return r
