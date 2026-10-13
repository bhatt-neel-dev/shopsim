"""payments: authorizations, captures, refunds and voids against a fake card gateway."""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Query
from opentelemetry import trace
from opentelemetry.metrics import Observation
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from shopsim.common.fakedata import now_iso
from shopsim.common.faults import FraudSuspected, NotFound, PaymentDeclined, checkpoint, jitter_ms

PEERS = ()
SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_ref TEXT NOT NULL UNIQUE,
    order_ref TEXT,
    customer_email TEXT,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    method TEXT NOT NULL,
    card_brand TEXT,
    card_last4 TEXT,
    status TEXT NOT NULL,
    gateway TEXT NOT NULL,
    gateway_txn TEXT,
    decline_reason TEXT,
    risk_score REAL,
    refunded_amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_ref);
"""
METHODS = ("card", "upi", "paypal", "wallet", "netbanking", "gift_card", "cod")
GATEWAY = "cardnet"


class CardIn(BaseModel):
    brand: str
    last4: str = Field(pattern=r"^\d{4}$")


class PaymentIn(BaseModel):
    order_ref: str = Field(min_length=3, max_length=40)
    amount: float = Field(gt=0, le=1_000_000)
    currency: str = Field("USD", min_length=3, max_length=3)
    method: Literal["card", "upi", "paypal", "wallet", "netbanking", "gift_card", "cod"] = "card"
    card: CardIn | None = None
    customer_email: str | None = None


class PaymentAction(BaseModel):
    action: Literal["capture", "refund"]
    amount: float | None = Field(None, gt=0)


def seed(ctx) -> None:
    if ctx.db.execute_sync("SELECT COUNT(*) AS n FROM payments").one()["n"]:
        return
    rng = ctx.fake.rng
    stamp = now_iso()
    rows = []
    for _ in range(150):
        method, card = ctx.fake.payment()
        status = rng.choices(["authorized", "captured", "settled", "refunded", "declined"], [20, 30, 35, 8, 7])[0]
        rows.append((f"PAY-{rng.randint(10**9, 10**10 - 1)}", f"ORD-{rng.randint(10**7, 10**8 - 1)}",
                     ctx.fake.customer()["email"], round(rng.uniform(5, 900), 2), "USD", method,
                     card["brand"] if card else None, card["last4"] if card else None, status, GATEWAY,
                     f"TXN-{uuid.uuid4().hex[:12].upper()}", "do_not_honor" if status == "declined" else None,
                     round(rng.betavariate(2, 9), 3), stamp, stamp))
    ctx.db.executemany_sync(
        "INSERT OR IGNORE INTO payments (payment_ref, order_ref, customer_email, amount, currency, method, "
        "card_brand, card_last4, status, gateway, gateway_txn, decline_reason, risk_score, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    ctx.logger.info("Seeded %d payments", len(rows), extra={"fields": {"event": "seed"}})


def _refresh_counts(ctx) -> None:
    row = ctx.db.execute_sync("SELECT COUNT(*) AS n, SUM(CASE WHEN status = 'captured' THEN 1 ELSE 0 END) AS "
                              "unsettled FROM payments").one()
    ctx.state.update(payments=row["n"], unsettled=row["unsettled"] or 0)


def setup(ctx):
    meter = ctx.meter
    _refresh_counts(ctx)
    ctx.state["processed"] = meter.create_counter("shopsim.payments.processed", unit="{payment}",
                                                  description="Payments by method and outcome")
    ctx.state["amount"] = meter.create_counter("shopsim.payments.amount", unit="USD",
                                               description="Amount authorized")
    ctx.state["declined"] = meter.create_counter("shopsim.payments.declined", unit="{payment}",
                                                 description="Declined or blocked payments, by reason")
    ctx.state["refunds"] = meter.create_counter("shopsim.payments.refunds", unit="{refund}", description="Refunds")
    ctx.state["gateway_latency"] = meter.create_histogram("shopsim.payments.gateway.duration", unit="ms",
                                                          description="Card gateway call duration")
    meter.create_observable_gauge("shopsim.payments.unsettled",
                                  callbacks=[lambda o: [Observation(ctx.state["unsettled"])]],
                                  description="Captured payments awaiting settlement")

    async def settlement():
        rows = (await ctx.db.query("SELECT id, amount FROM payments WHERE status = 'captured' ORDER BY id LIMIT 200",
                                   table="payments")).rows
        if not rows:
            return
        batch = f"STL-{random.randint(10**6, 10**7 - 1)}"
        with ctx.tracer.start_as_current_span("gateway.settlement_upload", kind=SpanKind.CLIENT,
                                              attributes={"peer.service": "cardnet-gateway", "settlement.batch": batch,
                                                          "settlement.count": len(rows)}):
            await asyncio.sleep(random.uniform(0.1, 0.6))
        ids = [r["id"] for r in rows]
        await ctx.db.query(f"UPDATE payments SET status = 'settled', updated_at = ? WHERE id IN "
                           f"({', '.join('?' * len(ids))})", (now_iso(), *ids), table="payments")
        ctx.logger.info("Settlement batch %s submitted: %d payments, %.2f USD", batch, len(rows),
                        sum(r["amount"] for r in rows), extra={"fields": {"event": "settlement.submitted",
                                                                          "settlement.batch": batch}})

    async def retention():
        pruned = ctx.db.prune("payments", ctx.runtime.eff.max_rows)
        _refresh_counts(ctx)
        if pruned:
            ctx.logger.info("Retention pruned %d old payments", pruned, extra={"fields": {"event": "retention"}})

    async def risk_review():
        row = (await ctx.db.query("SELECT COUNT(*) AS n FROM payments WHERE status = 'declined'",
                                  table="payments")).one()
        level = 30 if row["n"] > 200 else 20
        ctx.logger.log(level, "Risk review queue: %d declined payments on record", row["n"],
                       extra={"fields": {"event": "risk.review", "count": row["n"]}})

    return [("payments.settlement", 45, settlement), ("payments.retention", 60, retention),
            ("payments.risk_review", 50, risk_review)]


def router(ctx) -> APIRouter:
    r = APIRouter()
    db = ctx.db

    async def load(payment_id: int) -> dict:
        row = (await db.query("SELECT * FROM payments WHERE id = ?", (payment_id,), table="payments")).one()
        if row is None:
            raise NotFound(f"payment {payment_id} not found")
        return row

    async def gateway(operation: str, amount: float, method: str) -> str:
        with ctx.tracer.start_as_current_span(f"gateway.{operation}", kind=SpanKind.CLIENT, attributes={
            "peer.service": "cardnet-gateway", "server.address": "api.cardnet.example", "server.port": 443,
            "payment.method": method, "payment.amount": amount,
        }) as span:
            started = time.perf_counter()
            await asyncio.sleep(jitter_ms(70, 120) / 1000)
            await checkpoint(ctx, "gateway")
            txn = f"TXN-{uuid.uuid4().hex[:12].upper()}"
            span.set_attribute("payment.gateway.txn", txn)
            ctx.state["gateway_latency"].record((time.perf_counter() - started) * 1000, {"operation": operation})
            return txn

    async def insert(body: PaymentIn, ref: str, status: str, txn: str | None, risk: float,
                     decline: str | None = None) -> dict:
        stamp = now_iso()
        card = body.card
        res = await db.query(
            "INSERT INTO payments (payment_ref, order_ref, customer_email, amount, currency, method, card_brand, "
            "card_last4, status, gateway, gateway_txn, decline_reason, risk_score, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ref, body.order_ref, body.customer_email, body.amount, body.currency, body.method,
             card.brand if card else None, card.last4 if card else None, status, GATEWAY, txn, decline, risk,
             stamp, stamp), table="payments")
        return {"id": res.lastrowid, "payment_ref": ref, "order_ref": body.order_ref, "amount": body.amount,
                "currency": body.currency, "method": body.method, "status": status, "gateway_txn": txn,
                "risk_score": risk, "decline_reason": decline, "created_at": stamp}

    @r.get("/payments")
    async def list_payments(status: str | None = None, method: str | None = None, order_ref: str | None = None,
                            limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        await checkpoint(ctx, "start", resource="payment")
        sql, params = "SELECT * FROM payments WHERE 1 = 1", []
        for column, value in (("status", status), ("method", method), ("order_ref", order_ref)):
            if value:
                sql += f" AND {column} = ?"
                params.append(value)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = (await db.query(sql, params, table="payments")).rows
        await checkpoint(ctx, "logic")
        return {"items": rows, "count": len(rows), "offset": offset}

    @r.get("/payments/{payment_id}")
    async def get_payment(payment_id: int):
        await checkpoint(ctx, "start", resource="payment", id=payment_id)
        payment = await load(payment_id)
        await checkpoint(ctx, "logic")
        return payment

    @r.post("/payments", status_code=201)
    async def create_payment(body: PaymentIn):
        await checkpoint(ctx, "start", resource="payment")
        ref = f"PAY-{random.randint(10**9, 10**10 - 1)}"
        trace.get_current_span().set_attributes({"payment.ref": ref, "payment.method": body.method,
                                                 "payment.amount": body.amount, "order.number": body.order_ref})
        with ctx.tracer.start_as_current_span("risk.score") as span:
            await checkpoint(ctx, "logic")
            risk = round(random.betavariate(2, 9), 3)
            span.set_attribute("risk.score", risk)
        txn = None
        if body.method != "cod":
            try:
                txn = await gateway("authorize", body.amount, body.method)
            except (PaymentDeclined, FraudSuspected) as exc:
                reason = str(exc).split(": ")[-1] if isinstance(exc, PaymentDeclined) else "fraud_screen"
                await insert(body, ref, "declined", None, risk, reason)
                ctx.state["declined"].add(1, {"reason": reason, "payment.method": body.method})
                ctx.state["processed"].add(1, {"payment.method": body.method, "outcome": "declined"})
                raise
        status = "pending" if body.method == "cod" else "authorized"
        payment = await insert(body, ref, status, txn, risk)
        ctx.state["processed"].add(1, {"payment.method": body.method, "outcome": status})
        ctx.state["amount"].add(body.amount, {"payment.method": body.method})
        ctx.logger.info("Payment %s %s: %.2f %s via %s for %s", ref, status, body.amount, body.currency, body.method,
                        body.order_ref, extra={"fields": {"event": "payment.authorized", "payment.ref": ref,
                                                          "order.number": body.order_ref}})
        return payment

    @r.put("/payments/{payment_id}")
    async def update_payment(payment_id: int, body: PaymentAction):
        await checkpoint(ctx, "start", resource="payment", id=payment_id)
        payment = await load(payment_id)
        await checkpoint(ctx, "logic")
        status = payment["status"]
        if body.action == "capture" and status in ("authorized", "pending"):
            await gateway("capture", payment["amount"], payment["method"])
            payment.update(status="captured")
        elif body.action == "refund" and status in ("authorized", "captured", "settled"):
            amount = min(body.amount or payment["amount"], payment["amount"])
            await gateway("refund", amount, payment["method"])
            payment.update(status="refunded", refunded_amount=amount)
            ctx.state["refunds"].add(1, {"payment.method": payment["method"]})
        else:
            return payment  # already in that state: idempotent
        await db.query("UPDATE payments SET status = ?, refunded_amount = ?, updated_at = ? WHERE id = ?",
                       (payment["status"], payment["refunded_amount"], now_iso(), payment_id), table="payments")
        ctx.logger.info("Payment %s %s (was %s)", payment["payment_ref"], payment["status"], status,
                        extra={"fields": {"event": f"payment.{payment['status']}",
                                          "payment.ref": payment["payment_ref"]}})
        return payment

    @r.delete("/payments/{payment_id}")
    async def delete_payment(payment_id: int):
        await checkpoint(ctx, "start", resource="payment", id=payment_id)
        payment = await load(payment_id)
        await checkpoint(ctx, "logic")
        await db.query("DELETE FROM payments WHERE id = ?", (payment_id,), table="payments")
        ctx.logger.info("Payment %s voided and removed (was %s)", payment["payment_ref"], payment["status"],
                        extra={"fields": {"event": "payment.voided", "payment.ref": payment["payment_ref"]}})
        return {"deleted": payment_id, "payment_ref": payment["payment_ref"]}

    @r.get("/internal/ids", include_in_schema=False)
    async def ids(limit: int = 500):
        return [row["id"] for row in db.execute_sync("SELECT id FROM payments ORDER BY RANDOM() LIMIT ?",
                                                     (limit,)).rows]

    @r.get("/internal/recent", include_in_schema=False)
    async def recent(limit: int = 15):
        return db.execute_sync("SELECT * FROM payments ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).rows

    return r
