import json
import os

import pytest

from conftest import client

ORDER = {
    "customer": {"id": "CUS-100001", "name": "Asha Rao", "email": "asha@example.com", "country": "IN", "city": "Pune"},
    "items": [{"product_id": 1, "qty": 1}],
    "payment_method": "card",
    "card": {"brand": "visa", "last4": "4242"},
}


def spans(app):
    return app.state.ctx.telemetry.span_exporter.get_finished_spans()


async def restock(shop, product_id=1):
    async with client(shop["catalog"]) as c:
        assert (await c.put(f"/products/{product_id}", json={"stock": 500})).status_code == 200


async def test_catalog_crud(shop):
    async with client(shop["catalog"]) as c:
        listed = await c.get("/products", params={"limit": 5})
        assert listed.status_code == 200 and listed.json()["count"] == 5
        created = await c.post("/products", json={"name": "Test Lamp", "category": "Home & Kitchen",
                                                  "brand": "Lumen", "price": 19.5, "stock": 4})
        assert created.status_code == 201
        pid = created.json()["id"]
        assert (await c.put(f"/products/{pid}", json={"price": 21})).json()["price"] == 21
        assert (await c.get(f"/products/{pid}")).json()["name"] == "Test Lamp"
        assert (await c.delete(f"/products/{pid}")).status_code == 200
        assert (await c.get(f"/products/{pid}")).status_code == 404


async def test_checkout_is_one_trace_across_three_services(shop):
    await restock(shop)
    for app in shop.values():
        app.state.ctx.telemetry.span_exporter.clear()
    async with client(shop["orders"]) as o:
        resp = await o.post("/orders", json=ORDER)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "paid"
    root = next(s for s in spans(shop["orders"]) if s.name == "POST /orders")
    trace_id = root.context.trace_id
    assert any(s.context.trace_id == trace_id and s.name == "UPDATE products" for s in spans(shop["catalog"]))
    assert any(s.context.trace_id == trace_id and s.name == "gateway.authorize" for s in spans(shop["payments"]))


async def test_checkout_retries_transient_db_lock(shop):
    await restock(shop)
    eff = shop["orders"].state.ctx.runtime.eff
    eff.error_rate, eff.error_types = 1.0, {"db_lock": 1}
    async with client(shop["orders"]) as o:
        resp = await o.post("/orders", json=ORDER)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "paid"


async def test_order_update_cancel_refunds_and_delete(shop):
    await restock(shop)
    async with client(shop["orders"]) as o:
        order = (await o.post("/orders", json=ORDER)).json()
        assert (await o.put(f"/orders/{order['id']}", json={"status": "shipped"})).json()["status"] == "shipped"
        assert (await o.put(f"/orders/{order['id']}", json={"status": "cancelled"})).json()["status"] == "cancelled"
        assert (await o.delete(f"/orders/{order['id']}")).status_code == 200
    async with client(shop["payments"]) as p:
        payment = (await p.get("/payments", params={"order_ref": order["order_number"]})).json()["items"][0]
    assert payment["status"] == "refunded"


@pytest.mark.parametrize("fault,status,exc_type", [
    ("http_500", 500, "shopsim.common.faults.InternalError"),
    ("exception", 500, None),
    ("db_lock", 500, "sqlite3.OperationalError"),
    ("rate_limited", 429, None),
    ("not_found", 404, None),
])
async def test_injected_faults(shop, fault, status, exc_type):
    eff = shop["catalog"].state.ctx.runtime.eff
    eff.error_rate, eff.error_types, eff.timeout_ms = 1.0, {fault: 1}, 100
    shop["catalog"].state.ctx.telemetry.span_exporter.clear()
    async with client(shop["catalog"]) as c:
        resp = await c.get("/products/2")
    assert resp.status_code == status
    if status >= 500:
        server = next(s for s in spans(shop["catalog"]) if s.name == "GET /products/{product_id}")
        assert server.status.status_code.name == "ERROR"
        recorded = [e.attributes["exception.type"] for e in server.events if e.name == "exception"]
        assert recorded and (exc_type is None or exc_type in recorded)
        assert "stacktrace" in "".join(k for e in server.events for k in e.attributes)


async def test_error_rate_by_method_overrides_service_rate(shop):
    eff = shop["catalog"].state.ctx.runtime.eff
    eff.error_rate, eff.error_rate_by_method, eff.error_types = 0.0, {"GET": 1.0}, {"http_500": 1}
    async with client(shop["catalog"]) as c:
        assert (await c.get("/products/3")).status_code == 500
        assert (await c.put("/products/3", json={"rating": 4.1})).status_code == 200


async def test_card_decline_cascades_and_rolls_back_stock(shop):
    await restock(shop)
    pay = shop["payments"].state.ctx.runtime.eff
    pay.error_rate, pay.forced_error_type = 1.0, "card_declined"
    async with client(shop["orders"]) as o:
        resp = await o.post("/orders", json={**ORDER, "items": [{"product_id": 1, "qty": 3}]})
    assert resp.status_code == 402 and resp.json()["error"] == "card_declined"
    async with client(shop["catalog"]) as c:
        assert (await c.get("/products/1")).json()["stock"] == 500  # reservation released


async def test_degraded_dependency_gives_502_upstream(shop):
    await restock(shop)
    shop["catalog"].state.ctx.runtime.eff.state = "degraded"
    async with client(shop["orders"]) as o:
        resp = await o.post("/orders", json=ORDER)
    assert resp.status_code == 502
    async with client(shop["catalog"]) as c:
        health = await c.get("/health")
    assert health.status_code == 503


async def test_logs_are_json_with_trace_ids(shop):
    eff = shop["payments"].state.ctx.runtime.eff
    eff.error_rate, eff.error_types = 1.0, {"exception": 1}
    async with client(shop["payments"]) as p:
        assert (await p.get("/payments/1")).status_code == 500
    for handler in shop["payments"].state.ctx.logger.handlers:
        handler.flush()
    path = os.path.join(os.environ["SHOPSIM_LOG_DIR"], "payments.log")
    errors = [json.loads(line) for line in open(path) if '"event":"http.error"' in line]
    assert errors, "no error log written"
    last = errors[-1]
    assert len(last["trace_id"]) == 32 and last["error.stack_trace"].startswith("Traceback")


async def test_simulated_down_mutes_telemetry(shop):
    app = shop["catalog"]
    eff = app.state.ctx.runtime.eff
    eff.state, eff.down_mode = "down", "simulated"
    app.state.ctx.telemetry.span_exporter.clear()
    async with client(app) as c:
        assert (await c.get("/products/1")).status_code == 503
    assert spans(app) == ()
    eff.down_mode = "container"
