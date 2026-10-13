"""catalog: products, categories and stock. Orders reserve and release stock here."""
from __future__ import annotations

import asyncio
import random
from collections import OrderedDict

from fastapi import APIRouter, Query
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from shopsim.common.fakedata import now_iso
from shopsim.common.faults import NotFound, OutOfStock, checkpoint, current_injection

PEERS = ()
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    brand TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    stock INTEGER NOT NULL,
    rating REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
"""
COLUMNS = "id, sku, name, category, brand, price, currency, stock, rating, created_at, updated_at"
_CACHE_SIZE = 500


class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    category: str = Field(min_length=2, max_length=60)
    brand: str = Field(min_length=1, max_length=60)
    price: float = Field(gt=0, le=100_000)
    stock: int = Field(ge=0, le=100_000)
    currency: str = Field("USD", min_length=3, max_length=3)
    rating: float | None = Field(None, ge=0, le=5)
    sku: str | None = Field(None, max_length=40)


class ProductUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=200)
    category: str | None = Field(None, min_length=2, max_length=60)
    price: float | None = Field(None, gt=0, le=100_000)
    stock: int | None = Field(None, ge=0, le=100_000)
    rating: float | None = Field(None, ge=0, le=5)


class StockChange(BaseModel):
    qty: int = Field(ge=1, le=100)
    order_ref: str | None = None


def seed(ctx) -> None:
    if ctx.db.execute_sync("SELECT COUNT(*) AS n FROM products").one()["n"]:
        return
    stamp = now_iso()
    rows = []
    for _ in range(300):
        p = ctx.fake.product()
        rows.append((p["sku"], p["name"], p["category"], p["brand"], p["price"], p["currency"], p["stock"],
                     p["rating"], stamp, stamp))
    ctx.db.executemany_sync("INSERT INTO products (sku, name, category, brand, price, currency, stock, rating, "
                            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    ctx.logger.info("Seeded %d products", len(rows), extra={"fields": {"event": "seed"}})


def _refresh_counts(ctx) -> None:
    row = ctx.db.execute_sync("SELECT COUNT(*) AS n, COALESCE(SUM(stock), 0) AS units, "
                              "SUM(CASE WHEN stock < 10 THEN 1 ELSE 0 END) AS low FROM products").one()
    ctx.state.update(products=row["n"], units=row["units"], low_stock=row["low"] or 0)


def setup(ctx):
    meter = ctx.meter
    _refresh_counts(ctx)
    ctx.state["cache"] = OrderedDict()
    ctx.state["reservations"] = meter.create_counter("shopsim.catalog.reservations", unit="{reservation}",
                                                     description="Stock reservations by outcome")
    for name, key, desc in (("shopsim.catalog.products", "products", "Products in catalog"),
                            ("shopsim.catalog.stock.units", "units", "Units in stock across all products"),
                            ("shopsim.catalog.low_stock", "low_stock", "Products with fewer than 10 units")):
        meter.create_observable_gauge(name, callbacks=[lambda o, k=key: [Observation(ctx.state[k])]],
                                      description=desc)

    async def replenish():
        low = await ctx.db.query("SELECT id, sku, stock FROM products WHERE stock < 10 ORDER BY stock LIMIT 25",
                                 table="products")
        for row in low.rows:
            await ctx.db.query("UPDATE products SET stock = stock + ?, updated_at = ? WHERE id = ?",
                               (random.randint(50, 300), now_iso(), row["id"]), table="products")
        if low.rows:
            ctx.logger.info("Replenished %d low-stock SKUs from supplier", len(low.rows),
                            extra={"fields": {"event": "inventory.replenished", "count": len(low.rows)}})

    async def retention():
        pruned = ctx.db.prune("products", ctx.runtime.eff.max_rows)
        _refresh_counts(ctx)
        if pruned:
            ctx.logger.info("Retention pruned %d old products", pruned, extra={"fields": {"event": "retention"}})

    async def reindex():
        rows = await ctx.db.query("SELECT category, COUNT(*) AS n FROM products GROUP BY category",
                                  table="products")
        with ctx.tracer.start_as_current_span("search.index.build", kind=SpanKind.INTERNAL) as span:
            await asyncio.sleep(random.uniform(0.05, 0.4))
            span.set_attribute("search.documents", sum(r["n"] for r in rows.rows))
        ctx.logger.info("Search index rebuilt: %d documents in %d categories", sum(r["n"] for r in rows.rows),
                        len(rows.rows), extra={"fields": {"event": "search.reindexed"}})

    return [("inventory.replenish", 30, replenish), ("catalog.retention", 60, retention),
            ("search.reindex", 45, reindex)]


def router(ctx) -> APIRouter:
    r = APIRouter()
    db = ctx.db
    cache: OrderedDict = ctx.state["cache"]

    async def load(product_id: int) -> dict:
        with ctx.tracer.start_as_current_span("cache.get", kind=SpanKind.INTERNAL,
                                              attributes={"cache.key": f"product:{product_id}"}) as span:
            pending = current_injection()
            # A pending DB fault must reach the DB, so it counts as a miss (as if just evicted).
            db_fault = pending is not None and pending.type == "db_lock" and not pending.consumed
            hit = None if db_fault else cache.get(product_id)
            span.set_attribute("cache.hit", hit is not None)
        if hit is not None:
            cache.move_to_end(product_id)
            return dict(hit)
        row = (await db.query(f"SELECT {COLUMNS} FROM products WHERE id = ?", (product_id,), table="products")).one()
        if row is None:
            raise NotFound(f"product {product_id} not found")
        cache[product_id] = row
        if len(cache) > _CACHE_SIZE:
            cache.popitem(last=False)
        return dict(row)

    @r.get("/products")
    async def list_products(category: str | None = None, q: str | None = None,
                            limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        await checkpoint(ctx, "start", resource="product")
        sql, params = f"SELECT {COLUMNS} FROM products WHERE 1 = 1", []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if q:
            sql += " AND name LIKE ?"
            params.append(f"%{q}%")
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = (await db.query(sql, params, table="products")).rows
        await checkpoint(ctx, "logic")
        return {"items": rows, "count": len(rows), "offset": offset}

    @r.get("/products/{product_id}")
    async def get_product(product_id: int):
        await checkpoint(ctx, "start", resource="product", id=product_id)
        product = await load(product_id)
        await checkpoint(ctx, "logic")
        return product

    @r.post("/products", status_code=201)
    async def create_product(body: ProductIn):
        await checkpoint(ctx, "start", resource="product")
        await checkpoint(ctx, "logic")
        stamp = now_iso()
        sku = body.sku or ctx.fake.product()["sku"]
        res = await db.query(
            "INSERT INTO products (sku, name, category, brand, price, currency, stock, rating, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sku, body.name, body.category, body.brand, body.price, body.currency, body.stock, body.rating,
             stamp, stamp), table="products")
        ctx.logger.info("Product %s created: %s (%s) at %.2f", sku, body.name, body.category, body.price,
                        extra={"fields": {"event": "product.created", "product.id": res.lastrowid, "sku": sku}})
        return {"id": res.lastrowid, "sku": sku, **body.model_dump(exclude={"sku"}), "created_at": stamp}

    @r.put("/products/{product_id}")
    async def update_product(product_id: int, body: ProductUpdate):
        await checkpoint(ctx, "start", resource="product", id=product_id)
        product = await load(product_id)
        await checkpoint(ctx, "logic")
        changes = body.model_dump(exclude_none=True)
        if changes:
            changes["updated_at"] = now_iso()
            assignments = ", ".join(f"{k} = ?" for k in changes)
            await db.query(f"UPDATE products SET {assignments} WHERE id = ?", (*changes.values(), product_id),
                           table="products")
            cache.pop(product_id, None)
            if "price" in changes and product["price"]:
                delta = (changes["price"] - product["price"]) / product["price"] * 100
                ctx.logger.info("Price of %s changed %.2f -> %.2f (%+.1f%%)", product["sku"], product["price"],
                                changes["price"], delta, extra={"fields": {"event": "product.repriced",
                                                                          "sku": product["sku"]}})
        product.update(changes)
        return product

    @r.delete("/products/{product_id}")
    async def delete_product(product_id: int):
        await checkpoint(ctx, "start", resource="product", id=product_id)
        product = await load(product_id)
        await checkpoint(ctx, "logic")
        await db.query("DELETE FROM products WHERE id = ?", (product_id,), table="products")
        cache.pop(product_id, None)
        ctx.logger.info("Product %s deleted", product["sku"], extra={"fields": {"event": "product.deleted",
                                                                               "sku": product["sku"]}})
        return {"deleted": product_id, "sku": product["sku"]}

    @r.post("/products/{product_id}/reserve")
    async def reserve(product_id: int, body: StockChange):
        await checkpoint(ctx, "start", resource="product", id=product_id)
        product = await load(product_id)
        await checkpoint(ctx, "logic", sku=product["sku"])
        res = await db.query("UPDATE products SET stock = stock - ?, updated_at = ? WHERE id = ? AND stock >= ?",
                             (body.qty, now_iso(), product_id, body.qty), table="products")
        cache.pop(product_id, None)
        if res.rowcount == 0:
            ctx.state["reservations"].add(1, {"outcome": "out_of_stock"})
            raise OutOfStock(f"Insufficient stock for SKU {product['sku']}: requested {body.qty}, "
                             f"available {product['stock']}")
        ctx.state["reservations"].add(1, {"outcome": "reserved"})
        return {"id": product_id, "sku": product["sku"], "name": product["name"], "price": product["price"],
                "currency": product["currency"], "reserved": body.qty, "remaining": product["stock"] - body.qty}

    @r.post("/products/{product_id}/release")
    async def release(product_id: int, body: StockChange):
        await checkpoint(ctx, "start", resource="product", id=product_id)
        res = await db.query("UPDATE products SET stock = stock + ?, updated_at = ? WHERE id = ?",
                             (body.qty, now_iso(), product_id), table="products")
        cache.pop(product_id, None)
        ctx.state["reservations"].add(1, {"outcome": "released"})
        return {"id": product_id, "released": body.qty, "found": res.rowcount > 0}

    @r.get("/categories")
    async def categories():
        await checkpoint(ctx, "start", resource="category")
        rows = (await db.query("SELECT category, COUNT(*) AS products, ROUND(AVG(price), 2) AS avg_price, "
                               "SUM(stock) AS units FROM products GROUP BY category ORDER BY category",
                               table="products")).rows
        await checkpoint(ctx, "logic")
        return {"items": rows}

    @r.get("/internal/ids", include_in_schema=False)
    async def ids(limit: int = 500):
        return [row["id"] for row in db.execute_sync(
            "SELECT id FROM products WHERE stock > 0 ORDER BY RANDOM() LIMIT ?", (limit,)).rows]

    @r.get("/internal/recent", include_in_schema=False)
    async def recent(limit: int = 15):
        return db.execute_sync(f"SELECT {COLUMNS} FROM products ORDER BY updated_at DESC, id DESC LIMIT ?",
                               (limit,)).rows

    return r
