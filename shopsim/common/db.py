"""Real SQLite storage with a client span per query, configurable DB latency and the db_lock fault."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, field

from opentelemetry.trace import SpanKind

from shopsim.common import faults


DB_LOCK_RETRIES = 3
DB_LOCK_BACKOFF_SECONDS = 0.05


@dataclass
class Result:
    rows: list[dict] = field(default_factory=list)
    rowcount: int = 0
    lastrowid: int | None = None

    def one(self) -> dict | None:
        return self.rows[0] if self.rows else None


class Database:
    def __init__(self, ctx, path: str, schema: str):
        self.ctx = ctx
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.name = os.path.basename(path)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(schema)

    def execute_sync(self, sql: str, params: tuple | list = ()) -> Result:
        """Untraced, for seeding and internal endpoints."""
        cur = self.conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        return Result(rows, cur.rowcount, cur.lastrowid)

    def executemany_sync(self, sql: str, seq) -> None:
        with self.conn:
            self.conn.executemany(sql, seq)

    async def query(self, sql: str, params: tuple | list = (), *, table: str, op: str | None = None) -> Result:
        ctx = self.ctx
        op = op or sql.lstrip().split(None, 1)[0].upper()
        statement = " ".join(sql.split())
        with ctx.tracer.start_as_current_span(f"{op} {table}", kind=SpanKind.CLIENT, attributes={
            "db.system": "sqlite", "db.system.name": "sqlite",
            "db.name": self.name, "db.namespace": self.name,
            "db.statement": statement, "db.query.text": statement,
            "db.operation": op, "db.operation.name": op,
            "db.sql.table": table, "db.collection.name": table,
        }) as span:
            started = time.perf_counter()
            eff = ctx.runtime.eff
            delay = faults.jitter_ms(eff.db_latency_ms, eff.db_latency_ms * 0.3)
            if delay:
                await asyncio.sleep(delay / 1000)

            cur = None
            for attempt in range(DB_LOCK_RETRIES + 1):
                injected = faults.current_injection()
                try:
                    await faults.checkpoint(ctx, "db")
                    cur = self.conn.execute(sql, params)
                    break
                except sqlite3.OperationalError as exc:
                    # An injected db_lock is a deliberate one-shot fault used by the
                    # simulator and must remain observable. Only retry locks reported by
                    # SQLite itself after the fault checkpoint has passed.
                    if injected is not None and injected.type == "db_lock":
                        raise
                    locked = "locked" in str(exc).lower()
                    if not locked or attempt == DB_LOCK_RETRIES:
                        raise
                    backoff = DB_LOCK_BACKOFF_SECONDS * (2 ** attempt)
                    ctx.logger.warning(
                        "SQLite database locked; retrying %s %s (attempt %d/%d)",
                        op, table, attempt + 1, DB_LOCK_RETRIES,
                        extra={"fields": {"event": "db.lock_retry", "db.operation.name": op,
                                           "db.collection.name": table, "retry.attempt": attempt + 1}},
                    )
                    await asyncio.sleep(backoff)

            assert cur is not None
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            result = Result(rows, cur.rowcount, cur.lastrowid)
            span.set_attribute("db.response.returned_rows", len(rows))
            if cur.rowcount >= 0:
                span.set_attribute("db.response.affected_rows", cur.rowcount)
            elapsed = (time.perf_counter() - started) * 1000
            ctx.metrics.db_duration.record(elapsed, {"db.operation.name": op, "db.collection.name": table})
            if elapsed > 500:
                ctx.logger.warning("Slow query (%.0fms) on %s: %s", elapsed, table, statement[:200],
                                   extra={"fields": {"event": "db.slow_query", "duration_ms": round(elapsed, 1),
                                                     "db.collection.name": table}})
            return result

    def prune(self, table: str, keep: int) -> int:
        cur = self.conn.execute(
            f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM {table} ORDER BY id DESC LIMIT ?)", (keep,))
        return cur.rowcount
