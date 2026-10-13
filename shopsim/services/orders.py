"""orders: checkout and order lifecycle. Calls catalog (reserve/release stock) and payments
(charge/capture/refund), so one checkout is a trace across all three services."""
from __future__ import annotations

import json
import random
from typing import Literal

from fastapi import APIRouter, Query
from opentelemetry import trace
from opentelemetry.metrics import Observation
from pydantic import BaseModel, Field

from shopsim.common.downstream import error_message
from shopsim.common.fakedata import COUNTRIES, now_iso, tax_rate
from shopsim.common.faults import (
    DependencyError,
    FraudSuspected,
    NotFound,
    OutOfStock,
    PaymentDeclined,
    ValidationFailed,
    checkpoint,
    classify,
)

PEERS = ("catalog", "payments")
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    customer_name TEXT,
    customer_email TEXT,
    customer_tier TEXT,
    city TEXT,
    country TEXT,
    items TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    subtotal REAL NOT NULL,
    discount REAL NOT NULL DEFAULT 0,
    tax REAL NOT NULL,
    shipping REAL NOT NULL,
    total REAL NOT NULL,
    currency TEXT NOT NULL,
    payment_id INTEGER,
    payment_ref TEXT,
    payment_method TEXT,
    shipping_method TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
"""
STATUSES = ("pending", "paid", "processing", "shipped", "delivered", "cancelled")
SHIPPING = {"standard": 4.99, "express": 14.99, "same_day": 24.99}


class Customer(BaseModel):
    id: str = Field(min_length=3, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    tier: str = "standard"
    city: str = ""
    country: str = "US"


class Item(BaseModel):
    product_id: int = Field(ge=1)
    qty: int = Field(1, ge=1, le=20)


class Card(BaseModel):
    brand: str
    last4: str = Field(pattern=r"^\d{4}$")


class OrderIn(BaseModel):
    customer: Customer
    items: list[Item] = Field(min_length=1, max_length=10)
    payment_method: str = "card"
    card: Card | None = None
    currency: str = Field("USD", min_length=3, max_length=3)
    coupon: str | None = Field(None, max_length=30)
    shipping_method: Literal["standard", "express", "same_day"] = "standard"


class OrderUpdate(BaseModel):
    status: Literal["pending", "paid", "processing", "shipped", "delivered", "cancelled"]
    note: str | None = Field(None, max_length=200)


def _row_out(row: dict) -> dict:
    out = dict(row)
    out["items"] = json.loads(out["items"])
    return out


def seed(ctx) -> None:
    if ctx.db.execute_sync("SELECT COUNT(*) AS n FROM orders").one()["n"]:
        return
    rng = ctx.fake.rng
    stamp = now_iso()
    rows = []
    for _ in range(120):
        c = ctx.fake.customer()
        items = [{"product_id": rng.randint(1, 300), "qty": rng.randint(1, 3),
                  "price": round(rng.uniform(5, 400), 2)} for _ in range(rng.randint(1, 4))]
        subtotal = round(sum(i["price"] * i["qty"] for i in items), 2)
        tax = round(subtotal * tax_rate(c["country"]), 2)
        method, _ = ctx.fake.payment()
        rows.append((f"ORD-{rng.randint(10**7, 10**8 - 1)}", c["id"], c["name"], c["email"], c["tier"], c["city"],
                     c["country"], json.dumps(items), len(items), subtotal, 0, tax, 4.99,
                     round(subtotal + tax + 4.99, 2), "USD", None, None, method, "standard",
                     rng.choice(STATUSES), stamp, stamp))
    ctx.db.executemany_sync(
        "INSERT OR IGNORE INTO orders (order_number, customer_id, customer_name, customer_email, customer_tier, "
        "city, country, items, item_count, subtotal, discount, tax, shipping, total, currency, payment_id, "
        "payment_ref, payment_method, shipping_method, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    ctx.logger.info("Seeded %d orders", len(rows), extra={"fields": {"event": "seed"}})


def _refresh_counts(ctx) -> None:
    row = ctx.db.execute_sync("SELECT COUNT(*) AS n, SUM(CASE WHEN status IN ('pending', 'paid', 'processing') "
                              "THEN 1 ELSE 0 END) AS open FROM orders").one()
    ctx.state.update(orders=row["n"], open=row["open"] or 0)


def setup(ctx):
    meter = ctx.meter
    _refresh_counts(ctx)
    ctx.state["placed"] = meter.create_counter("shopsim.orders.placed", unit="{order}", description="Orders placed")
    ctx.state["failed"] = meter.create_counter("shopsim.orders.failed", unit="{order}",
                                               description="Checkouts that failed, by reason")
    ctx.state["revenue"] = meter.create_counter("shopsim.orders.revenue", unit="USD", description="Order revenue")
    ctx.state["value"] = meter.create_histogram("shopsim.orders.value", unit="USD", description="Order value")
    ctx.state["transitions"] = meter.create_counter("shopsim.orders.status_changes", unit="{change}",
                                                    description="Order status changes")
    meter.create_observable_gauge("shopsim.orders.open", callbacks=[lambda o: [Observation(ctx.state["open"])]],
                                  description="Orders pending, paid or processing")
    meter.create_observable_gauge("shopsim.orders.total", callbacks=[lambda o: [Observation(ctx.state["orders"])]],
                                  description="Orders stored")

    async def fulfillment():
        rows = (await ctx.db.query("SELECT id FROM orders WHERE status = 'paid' ORDER BY id LIMIT 20",
                                   table="orders")).rows
        for row in rows:
            await ctx.db.query("UPDATE orders SET status = 'processing', updated_at = ? WHERE id = ?",
                               (now_iso(), row["id"]), table="orders")
        if rows:
            ctx.logger.info("Fulfillment picked %d paid orders for packing", len(rows),
                            extra={"fields": {"event": "fulfillment.picked", "count": len(rows)}})

    async def retention():
        pruned = ctx.db.prune("orders", ctx.runtime.eff.max_rows)
        _refresh_counts(ctx)
        if pruned:
            ctx.logger.info("Retention pruned %d old orders", pruned, extra={"fields": {"event": "retention"}})

    async def reminders():
        row = (await ctx.db.query("SELECT COUNT(*) AS n FROM orders WHERE status = 'pending'",
                                  table="orders")).one()
        ctx.logger.info("Queued %d payment reminders for pending orders", row["n"],
                        extra={"fields": {"event": "orders.reminders", "count": row["n"]}})

    return [("orders.fulfillment", 30, fulfillment), ("orders.retention", 60, retention),
            ("orders.reminders", 40, reminders)]


def router(ctx) -> APIRouter:
    r = APIRouter()
    db = ctx.db
    catalog = ctx.downstream["catalog"]
    payments = ctx.downstream["payments"]

    async def load(order_id: int) -> dict:
        row = (await db.query("SELECT * FROM orders WHERE id = ?", (order_id,), table="orders")).one()
        if row is None:
            raise NotFound(f"order {order_id} not found")
        return row

    async def release(number: str, lines: list[dict], headers: dict) -> None:
        for line in lines:
            try:
                await catalog.call("POST", f"/products/{line['id']}/release", route="/products/{product_id}/release",
                                   json={"qty": line["qty"], "order_ref": number}, user_headers=headers)
            except Exception as exc:  # noqa: BLE001 - compensation is best effort
                ctx.logger.error("Could not release %d x %s for %s: %s", line["qty"], line.get("sku"), number, exc,
                                 extra={"fields": {"event": "order.compensation_failed", "order.number": number}})

    async def compensate(number: str, reserved: list[dict], payment: dict | None, exc: Exception,
                         headers: dict) -> None:
        if not reserved and payment is None:
            return
        with ctx.tracer.start_as_current_span("order.compensate", attributes={"order.number": number}):
            await release(number, reserved, headers)
            if payment is not None:
                try:
                    await payments.call("DELETE", f"/payments/{payment['id']}", route="/payments/{payment_id}",
                                        user_headers=headers)
                except Exception as void_exc:  # noqa: BLE001
                    ctx.logger.error("Could not void payment %s for %s: %s", payment.get("payment_ref"), number,
                                     void_exc, extra={"fields": {"event": "order.compensation_failed"}})
        ctx.logger.warning("Checkout %s rolled back after %s: released %d reservation(s)%s", number,
                           type(exc).__name__, len(reserved), ", voided payment" if payment else "",
                           extra={"fields": {"event": "order.rolled_back", "order.number": number}})

    @r.get("/orders")
    async def list_orders(status: str | None = None, customer_id: str | None = None,
                          limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        await checkpoint(ctx, "start", resource="order")
        sql, params = "SELECT * FROM orders WHERE 1 = 1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if customer_id:
            sql += " AND customer_id = ?"
            params.append(customer_id)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = (await db.query(sql, params, table="orders")).rows
        await checkpoint(ctx, "logic")
        return {"items": [_row_out(row) for row in rows], "count": len(rows), "offset": offset}

    @r.get("/orders/{order_id}")
    async def get_order(order_id: int):
        await checkpoint(ctx, "start", resource="order", id=order_id)
        order = await load(order_id)
        await checkpoint(ctx, "logic")
        return _row_out(order)

    @r.post("/orders", status_code=201)
    async def create_order(body: OrderIn):
        await checkpoint(ctx, "start", resource="order")
        number = f"ORD-{random.randint(10**7, 10**8 - 1)}"
        headers = {"x-user-id": body.customer.id}
        trace.get_current_span().set_attributes({"order.number": number, "order.items": len(body.items),
                                                 "payment.method": body.payment_method})
        reserved: list[dict] = []
        payment: dict | None = None
        try:
            with ctx.tracer.start_as_current_span("inventory.reserve", attributes={"order.items": len(body.items)}):
                for item in body.items:
                    resp = await catalog.call("POST", f"/products/{item.product_id}/reserve",
                                              route="/products/{product_id}/reserve",
                                              json={"qty": item.qty, "order_ref": number}, user_headers=headers)
                    if resp.status_code == 404:
                        ctx.logger.warning("Product %d is gone; dropped from %s", item.product_id, number,
                                           extra={"fields": {"event": "order.item_dropped", "order.number": number}})
                        continue
                    if resp.status_code == 409:
                        raise OutOfStock(error_message(resp))
                    if resp.status_code >= 400:
                        raise DependencyError(f"catalog rejected reservation ({resp.status_code}): "
                                              f"{error_message(resp)}")
                    line = resp.json()
                    line["qty"] = item.qty
                    reserved.append(line)
            if not reserved:
                raise ValidationFailed(f"None of the {len(body.items)} requested products are available")

            with ctx.tracer.start_as_current_span("order.pricing") as span:
                await checkpoint(ctx, "logic", sku=reserved[0]["sku"])
                subtotal = round(sum(line["price"] * line["qty"] for line in reserved), 2)
                discount = round(subtotal * 0.1, 2) if body.coupon else 0.0
                country = body.customer.country if body.customer.country in COUNTRIES else "US"
                tax = round((subtotal - discount) * tax_rate(country), 2)
                shipping = 0.0 if subtotal >= 100 and body.shipping_method == "standard" else SHIPPING[
                    body.shipping_method]
                total = round(subtotal - discount + tax + shipping, 2)
                span.set_attributes({"order.subtotal": subtotal, "order.total": total, "order.tax": tax})

            resp = await payments.call("POST", "/payments", route="/payments", user_headers=headers, json={
                "order_ref": number, "amount": total, "currency": body.currency, "method": body.payment_method,
                "card": body.card.model_dump() if body.card else None, "customer_email": body.customer.email})
            if resp.status_code == 402:
                raise PaymentDeclined(f"Payment for {number} declined: {error_message(resp)}")
            if resp.status_code == 403:
                raise FraudSuspected(f"Payment for {number} blocked: {error_message(resp)}")
            if resp.status_code >= 400:
                raise DependencyError(f"payments rejected charge ({resp.status_code}): {error_message(resp)}")
            payment = resp.json()

            stamp = now_iso()
            status = "pending" if payment["status"] == "pending" else "paid"
            items = [{"product_id": l["id"], "sku": l["sku"], "name": l["name"], "qty": l["qty"],
                      "price": l["price"]} for l in reserved]
            res = await db.query(
                "INSERT INTO orders (order_number, customer_id, customer_name, customer_email, customer_tier, city, "
                "country, items, item_count, subtotal, discount, tax, shipping, total, currency, payment_id, "
                "payment_ref, payment_method, shipping_method, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (number, body.customer.id, body.customer.name, body.customer.email, body.customer.tier,
                 body.customer.city, country, json.dumps(items), len(items), subtotal, discount, tax, shipping, total,
                 body.currency, payment["id"], payment["payment_ref"], body.payment_method, body.shipping_method,
                 status, stamp, stamp), table="orders")
        except Exception as exc:
            ctx.state["failed"].add(1, {"reason": classify(exc)[1]})
            await compensate(number, reserved, payment, exc, headers)
            raise

        attrs = {"payment.method": body.payment_method, "customer.country": country}
        ctx.state["placed"].add(1, attrs)
        ctx.state["revenue"].add(total, attrs)
        ctx.state["value"].record(total, attrs)
        ctx.logger.info("Order %s placed by %s: %d item(s), total %.2f %s via %s", number, body.customer.id,
                        len(items), total, body.currency, body.payment_method, extra={"fields": {
                            "event": "order.placed", "order.number": number, "order.total": total,
                            "customer.id": body.customer.id, "customer.country": country}})
        return {"id": res.lastrowid, "order_number": number, "status": status, "items": items,
                "subtotal": subtotal, "discount": discount, "tax": tax, "shipping": shipping, "total": total,
                "currency": body.currency, "payment_ref": payment["payment_ref"], "created_at": stamp}

    @r.put("/orders/{order_id}")
    async def update_order(order_id: int, body: OrderUpdate):
        await checkpoint(ctx, "start", resource="order", id=order_id)
        order = await load(order_id)
        await checkpoint(ctx, "logic")
        old, new = order["status"], body.status
        number = order["order_number"]
        headers = {"x-user-id": order["customer_id"]}
        trace.get_current_span().set_attributes({"order.number": number, "order.status.from": old,
                                                 "order.status.to": new})
        pid = order["payment_id"]
        if pid:
            if new == "cancelled" and old != "cancelled":
                resp = await payments.call("PUT", f"/payments/{pid}", route="/payments/{payment_id}",
                                           json={"action": "refund"}, user_headers=headers)
            elif new == "shipped":
                resp = await payments.call("PUT", f"/payments/{pid}", route="/payments/{payment_id}",
                                           json={"action": "capture"}, user_headers=headers)
            else:
                resp = await payments.call("GET", f"/payments/{pid}", route="/payments/{payment_id}",
                                           user_headers=headers)
            if resp.status_code == 404:
                ctx.logger.warning("Payment %s for %s no longer exists", pid, number,
                                   extra={"fields": {"event": "order.payment_missing", "order.number": number}})
            elif resp.status_code >= 400:
                raise DependencyError(f"payments refused {new} for {number} ({resp.status_code}): "
                                      f"{error_message(resp)}")
        else:
            await payments.call("GET", "/payments", route="/payments", params={"order_ref": number, "limit": 1},
                                user_headers=headers)
        if new == "cancelled" and old != "cancelled":
            lines = [{"id": i["product_id"], "qty": i["qty"], "sku": i.get("sku")} for i in json.loads(order["items"])]
            await release(number, lines, headers)
        await db.query("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (new, now_iso(), order_id),
                       table="orders")
        ctx.state["transitions"].add(1, {"order.status": new})
        ctx.logger.info("Order %s status %s -> %s", number, old, new, extra={"fields": {
            "event": "order.status_changed", "order.number": number, "order.status": new}})
        order.update(status=new)
        return _row_out(order)

    @r.delete("/orders/{order_id}")
    async def delete_order(order_id: int):
        await checkpoint(ctx, "start", resource="order", id=order_id)
        order = await load(order_id)
        await checkpoint(ctx, "logic")
        await db.query("DELETE FROM orders WHERE id = ?", (order_id,), table="orders")
        ctx.logger.info("Order %s deleted (status was %s)", order["order_number"], order["status"],
                        extra={"fields": {"event": "order.deleted", "order.number": order["order_number"]}})
        return {"deleted": order_id, "order_number": order["order_number"]}

    @r.get("/internal/ids", include_in_schema=False)
    async def ids(limit: int = 500):
        return [row["id"] for row in db.execute_sync("SELECT id FROM orders ORDER BY RANDOM() LIMIT ?",
                                                     (limit,)).rows]

    @r.get("/internal/recent", include_in_schema=False)
    async def recent(limit: int = 15):
        rows = db.execute_sync("SELECT * FROM orders ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).rows
        return [_row_out(row) for row in rows]

    return r
