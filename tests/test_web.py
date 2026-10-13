import json

import httpx

from conftest import client
from shopsim.config import FrontendEffective, ShopConfig, Spike, SpikeSpec, compute_effective, default_config

CUSTOMER = {"id": "CUS-200001", "name": "Mei Chen", "email": "mei@example.com", "country": "SG", "city": "Singapore"}
BROWSER_TRACE = "0000000000000000" + "4bf92f3577b34da6"


def spans(app):
    return app.state.ctx.telemetry.span_exporter.get_finished_spans()


def clear(shop):
    for app in shop.values():
        app.state.ctx.telemetry.span_exporter.clear()


async def test_pages_assets_and_health(shop):
    async with client(shop["web"]) as c:
        page = await c.get("/product/3")
        assert page.status_code == 200 and "window.SHOPSIM = {" in page.text and "__RUM_TAG__" not in page.text
        assert (await c.get("/nope.php")).status_code == 404
        assert (await c.get("/static/app.js")).status_code == 200
        assert (await c.get("/static/../services/web.py")).status_code == 404
        assert (await c.get("/img/p-3-lamp.svg")).headers["content-type"].startswith("image/svg")
        assert (await c.get("/img/missing-3.png")).status_code == 404
        health = await c.get("/health")
        assert health.status_code == 200 and health.json()["rum"]["enabled"] is False


async def test_browser_call_joins_the_trace_across_services(shop):
    clear(shop)
    traceparent = f"00-{BROWSER_TRACE}-00f067aa0ba902b7-01"
    async with client(shop["web"]) as c:
        resp = await c.get("/api/catalog/products", params={"limit": 3}, headers={"traceparent": traceparent})
    assert resp.status_code == 200 and resp.json()["count"] == 3
    web_server = [s for s in spans(shop["web"]) if s.name.startswith("GET /api/")][0]
    catalog_server = [s for s in spans(shop["catalog"]) if s.name == "GET /products"][0]
    assert format(web_server.context.trace_id, "032x") == BROWSER_TRACE
    assert catalog_server.context.trace_id == web_server.context.trace_id
    assert format(web_server.parent.span_id, "016x") == "00f067aa0ba902b7"


async def test_cart_and_checkout_through_the_storefront(shop):
    async with client(shop["catalog"]) as c:
        assert (await c.put("/products/2", json={"stock": 500})).status_code == 200
    clear(shop)
    async with client(shop["web"]) as c:
        added = await c.post("/api/cart/items", json={"product_id": 2, "qty": 2})
        assert added.status_code == 201 and added.json()["count"] == 2
        cart_id = added.json()["cart_id"]
        assert c.cookies.get("shopsim_cart") == cart_id
        order = await c.post("/api/checkout", json={"customer": CUSTOMER, "card": {"brand": "visa", "last4": "4242"}})
        assert order.status_code == 201, order.text
        assert order.json()["order_number"].startswith("ORD-")
        assert (await c.get("/api/cart")).json()["count"] == 0
        mine = await c.get("/api/orders/orders", params={"customer_id": CUSTOMER["id"]})
        assert mine.json()["items"][0]["order_number"] == order.json()["order_number"]
    checkout = [s for s in spans(shop["web"]) if s.name == "POST /api/checkout"][0]
    orders_span = [s for s in spans(shop["orders"]) if s.name == "POST /orders"][0]
    assert orders_span.context.trace_id == checkout.context.trace_id
    # The request's log lines ride along on its span.
    add = [s for s in spans(shop["web"]) if s.name == "POST /api/cart/items"][0]
    messages = [e.attributes.get("log.message", "") for e in add.events if e.name == "log"]
    assert any("Added 2 x" in m for m in messages)


async def test_empty_checkout_and_backend_errors_reach_the_browser(shop):
    async with client(shop["web"]) as c:
        empty = await c.post("/api/checkout", json={"customer": CUSTOMER}, headers={"x-cart-id": "CART-empty"})
        assert empty.status_code == 422
        assert (await c.get("/api/inventory/x")).status_code == 404
    eff = shop["catalog"].state.ctx.runtime.eff
    eff.error_rate, eff.forced_error_type = 1.0, "http_500"
    try:
        async with client(shop["web"]) as c:
            assert (await c.get("/api/catalog/products")).status_code == 500
    finally:
        eff.error_rate, eff.forced_error_type = 0, None


async def test_faults_only_hit_the_api(shop):
    eff = shop["web"].state.ctx.runtime.eff
    eff.error_rate, eff.forced_error_type = 1.0, "http_500"
    try:
        async with client(shop["web"]) as c:
            assert (await c.get("/")).status_code == 200
            assert (await c.get("/static/app.css")).status_code == 200
            assert (await c.get("/api/cart")).status_code == 500
    finally:
        eff.error_rate, eff.forced_error_type = 0, None


async def test_rum_relay_forwards_and_completes_open_views(shop):
    relay = shop["web"].state.ctx.state["relay"]
    sent: list[httpx.Request] = []

    def intake(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={})

    relay.intake = "https://rum.example:9477"
    relay.client = httpx.AsyncClient(transport=httpx.MockTransport(intake))
    try:
        view = {"type": "view", "session": {"id": "s-1"}, "_md": {"document_version": 3},
                "view": {"id": "v-1", "name": "/cart", "is_view_completed": "no", "is_active": True,
                         "loading_time": 820_000_000, "error": {"count": 1}}}
        beacon = json.dumps({"type": "resource", "resource": {}}) + "\n" + json.dumps(view)
        async with client(shop["web"]) as c:
            bad = await c.post("/rum", params={"mdforward": "/etc/passwd"}, content=b"x")
            assert bad.status_code == 400
            resp = await c.post("/rum", params={"mdforward": "/api/v2/rum?md-api-key=pub1"}, content=beacon,
                                headers={"x-forwarded-for": "49.36.10.20", "user-agent": "UA/1"})
        assert resp.status_code == 202
        assert str(sent[0].url) == "https://rum.example:9477/api/v2/rum?md-api-key=pub1"
        assert sent[0].headers["X-Forwarded-For"] == "49.36.10.20" and sent[0].headers["User-Agent"] == "UA/1"
        assert relay.event_counts == {"resource": 1, "view": 1} and "v-1" in relay.pending

        assert await relay.promote_once(now=relay.pending["v-1"]["seen"] + 10) == 0
        assert await relay.promote_once(now=relay.pending["v-1"]["seen"] + 60) == 1
        promoted = json.loads(sent[-1].content)
        assert promoted["view"]["is_view_completed"] == "yes" and promoted["view"]["is_active"] is False
        assert promoted["_md"]["document_version"] == 4 and not relay.pending
        assert list(relay.recent)[0]["completed"] == "relay"

        # A view the SDK finishes itself is not sent twice.
        view["view"]["id"] = "v-2"
        async with client(shop["web"]) as c:
            await c.post("/rum", params={"mdforward": "/api/v2/rum"}, content=json.dumps(view))
            view["view"]["is_view_completed"] = "yes"
            await c.post("/rum", params={"mdforward": "/api/v2/rum"}, content=json.dumps(view))
        assert not relay.pending and relay.stats["views_completed_by_sdk"] == 1
    finally:
        relay.intake = ""


def test_frontend_spikes_and_effective():
    cfg = default_config()

    def active(**spec) -> Spike:
        spike = Spike(**SpikeSpec(**spec).model_dump(), id=spec["kind"], created_at=0)
        spike.activate(0)
        return spike

    spikes = [active(kind="js_errors", target="all", value=0.7), active(kind="slow_frontend", value=900),
              active(kind="rum_traffic", value=3), active(kind="traffic", target="all", value=2)]
    assert all(sp.target == "web" for sp in spikes[:3])
    fe = compute_effective(cfg, spikes, "web").frontend
    assert fe.js_error_rate == 0.7 and fe.slow_render_ms == 900 and fe.slow_render_rate >= 0.8
    assert fe.sessions_per_min == cfg.rum.sessions_per_min * 6 and len(fe.active_spikes) == 4
    assert compute_effective(cfg, spikes, "orders").frontend is None
    assert isinstance(compute_effective(cfg, [], "web").frontend, FrontendEffective)


def test_frontend_spike_cannot_target_a_backend():
    try:
        SpikeSpec(kind="broken_assets", target="payments")
    except ValueError as exc:
        assert "storefront" in str(exc)
    else:
        raise AssertionError("expected a validation error")


def test_config_saved_before_web_existed_still_loads():
    old = default_config().model_dump(mode="json")
    del old["services"]["web"], old["rum"]
    del old["telemetry"]["log_span_events"]
    cfg = ShopConfig.model_validate(old)
    assert cfg.services["web"].error_types["dependency"] > 0 and cfg.rum.enabled
