"""Continuous synthetic traffic: GET/POST/PUT/DELETE against the services at the configured rate
and CRUD ratio (times any active traffic spikes), plus live per-service stats. Requests for `web`
go through the storefront's API (/api/<service>/...), like a mobile app's would."""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections import deque

import httpx

from shopsim.common.fakedata import CATEGORIES, FakeData
from shopsim.config import BACKENDS, SERVICES

_TICK = 0.05
_ORDER_STATUSES = {"processing": 30, "shipped": 35, "delivered": 25, "cancelled": 10}
_SEARCH_WORDS = ["Pro", "Wireless", "Max", "Smart", "Mini", "Organic", "Leather", "Kit", "Set", "Air"]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(pct / 100 * len(ordered)))], 1)


class Stats:
    """Rolling 60s window per service + a 5s-bucket series for sparklines (10 minutes)."""

    def __init__(self):
        self.window = {s: deque() for s in SERVICES}
        self.series = {s: deque(maxlen=120) for s in SERVICES}
        self.totals = {s: {"sent": 0, "errors": 0} for s in SERVICES}
        self.dropped = 0

    def record(self, service: str, method: str, status: int, ms: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.window[service].append((now, method, status, ms))
        self.totals[service]["sent"] += 1
        if status >= 500 or status <= 0:
            self.totals[service]["errors"] += 1

    def _prune(self, now: float) -> None:
        for events in self.window.values():
            while events and now - events[0][0] > 60:
                events.popleft()

    def snapshot(self, service: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._prune(now)
        events = self.window[service]
        recent = [e for e in events if now - e[0] <= 15]
        errors = sum(1 for e in recent if e[2] >= 500 or e[2] <= 0)
        client_errors = sum(1 for e in recent if 400 <= e[2] < 500)
        latencies = [e[3] for e in events if e[2] > 0]
        codes: dict[str, int] = {}
        methods: dict[str, int] = {}
        for _, method, status, _ in events:
            key = "conn_error" if status == 0 else "timeout" if status < 0 else str(status)
            codes[key] = codes.get(key, 0) + 1
            methods[method] = methods.get(method, 0) + 1
        return {
            "rps": round(len(recent) / 15, 2),
            "error_pct": round(100 * errors / len(recent), 1) if recent else 0.0,
            "client_error_pct": round(100 * client_errors / len(recent), 1) if recent else 0.0,
            "p50_ms": _percentile(latencies, 50), "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
            "codes": dict(sorted(codes.items())), "methods": methods, **self.totals[service],
        }

    def sample(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._prune(now)
        for service, events in self.window.items():
            recent = [e for e in events if now - e[0] <= 5]
            errors = sum(1 for e in recent if e[2] >= 500 or e[2] <= 0)
            self.series[service].append({
                "t": round(now), "rps": round(len(recent) / 5, 2),
                "err": round(100 * errors / len(recent), 1) if recent else 0.0,
                "p95": _percentile([e[3] for e in recent if e[2] > 0], 95),
            })


class TrafficGenerator:
    def __init__(self, store, urls: dict[str, str], max_total_rps: float,
                 transports: dict[str, httpx.AsyncBaseTransport] | None = None):
        self.store = store
        self.max_total_rps = max_total_rps
        self.fake = FakeData()
        self.rng = self.fake.rng
        self.stats = Stats()
        self.inflight = 0
        self.ids: dict[str, list[int]] = {s: [] for s in SERVICES}
        self.customers = [self.fake.customer() for _ in range(400)]
        transports = transports or {}
        self.clients = {s: httpx.AsyncClient(base_url=urls[s], transport=transports.get(s),
                                             limits=httpx.Limits(max_connections=500, max_keepalive_connections=100))
                        for s in SERVICES}

    # ---- rates -----------------------------------------------------------------------------------
    def rates(self) -> dict[str, float]:
        t = self.store.config.traffic
        if not t.enabled:
            return {s: 0.0 for s in SERVICES}
        total_weight = sum(t.service_weights.get(s, 0) for s in SERVICES) or 1
        rates = {s: t.rps * t.service_weights.get(s, 0) / total_weight * self.store.traffic_multiplier(s)
                 for s in SERVICES}
        total = sum(rates.values())
        if total > self.max_total_rps:
            rates = {s: r * self.max_total_rps / total for s, r in rates.items()}
        return rates

    def pick_method(self) -> str:
        ratio = {m: w for m, w in self.store.config.traffic.crud_ratio.items() if w > 0}
        if not ratio:
            return "GET"
        return self.rng.choices(list(ratio), weights=list(ratio.values()))[0]

    # ---- loops -----------------------------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        refresher = loop.create_task(self._refresh_ids_forever())
        sampler = loop.create_task(self._sample_forever())
        next_tick = loop.time()
        try:
            while True:
                next_tick += _TICK
                delay = next_tick - loop.time()
                if delay < -1:  # fell far behind (laptop sleep, GC): don't burst to catch up
                    next_tick = loop.time()
                await asyncio.sleep(max(0.0, delay))
                max_inflight = self.store.config.traffic.max_inflight
                for service, rate in self.rates().items():
                    expected = rate * _TICK
                    n = int(expected) + (1 if self.rng.random() < expected - int(expected) else 0)
                    for _ in range(n):
                        if self.inflight >= max_inflight:
                            self.stats.dropped += 1
                            continue
                        loop.create_task(self.fire(service, self.pick_method()))
        finally:
            refresher.cancel()
            sampler.cancel()

    async def _sample_forever(self) -> None:
        while True:
            await asyncio.sleep(5)
            self.stats.sample()

    async def _refresh_ids_forever(self) -> None:
        while True:
            await self.refresh_ids()
            await asyncio.sleep(15)

    async def refresh_ids(self) -> None:
        for service, client in self.clients.items():
            try:
                resp = await client.get("/internal/ids", params={"limit": 500}, timeout=5)
                if resp.status_code == 200:
                    self.ids[service] = resp.json()
            except httpx.HTTPError:
                pass

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()

    # ---- requests --------------------------------------------------------------------------------
    async def fire(self, service: str, method: str) -> None:
        self.inflight += 1
        started = time.perf_counter()
        status = 0
        try:
            http_method, path, params, body = self.build(service, method)
            method = http_method
            customer = self.rng.choice(self.customers)
            headers = {"user-agent": self.fake.user_agent(), "x-forwarded-for": self.fake.client_ip(),
                       "x-user-id": customer["id"], "x-session-id": uuid.uuid4().hex[:16],
                       "x-request-id": str(uuid.uuid4())}
            resp = await self.clients[service].request(http_method, path, params=params, json=body, headers=headers,
                                                       timeout=self.store.config.traffic.request_timeout_s)
            status = resp.status_code
            self._learn(service, http_method, path, resp)
        except httpx.TimeoutException:
            status = -1
        except httpx.HTTPError:
            status = 0
        finally:
            self.inflight -= 1
            self.stats.record(service, method, status, (time.perf_counter() - started) * 1000)

    def _learn(self, service: str, method: str, path: str, resp: httpx.Response) -> None:
        pool = self.ids[service]
        if method == "POST" and resp.status_code == 201 and path.count("/") == 1:
            new_id = resp.json().get("id")
            if new_id:
                pool.append(new_id)
                if len(pool) > 1000:
                    del pool[: len(pool) - 1000]
        elif method == "DELETE" and resp.status_code in (200, 404):
            try:
                pool.remove(int(path.rsplit("/", 1)[1]))
            except ValueError:
                pass

    def _pick_id(self, service: str) -> int | None:
        pool = self.ids[service]
        return self.rng.choice(pool) if pool else None

    def build(self, service: str, method: str) -> tuple[str, str, dict | None, dict | None]:
        """(http method, path, query params, json body) for one synthetic request."""
        rng = self.rng
        if service == "web":
            weights = self.store.config.traffic.service_weights
            backend = rng.choices(BACKENDS, weights=[max(weights.get(b, 0), 0.01) for b in BACKENDS])[0]
            http_method, path, params, body = self.build(backend, method)
            return http_method, f"/api/{backend}{path}", params, body
        resource = {"catalog": "products", "orders": "orders", "payments": "payments"}[service]
        target = self._pick_id(service)
        if method in ("PUT", "DELETE") and target is None:
            method = "GET"
        if method == "GET":
            roll = rng.random()
            if service == "catalog" and roll > 0.88:
                return "GET", "/categories", None, None
            if roll < 0.5 or target is None:
                params: dict = {"limit": rng.choice([10, 20, 50]), "offset": rng.choice([0, 0, 0, 20, 40])}
                if service == "catalog":
                    if rng.random() < 0.4:
                        params["category"] = rng.choice(list(CATEGORIES))
                    if rng.random() < 0.3:
                        params["q"] = rng.choice(_SEARCH_WORDS)
                elif service == "orders" and rng.random() < 0.4:
                    params["status"] = rng.choice(["paid", "processing", "shipped", "pending"])
                elif service == "payments" and rng.random() < 0.4:
                    params["status"] = rng.choice(["authorized", "captured", "settled", "refunded"])
                return "GET", f"/{resource}", params, None
            return "GET", f"/{resource}/{target}", None, None
        if method == "DELETE":
            return "DELETE", f"/{resource}/{target}", None, None
        if method == "PUT":
            return "PUT", f"/{resource}/{target}", None, self._update_body(service)
        return "POST", f"/{resource}", None, self._create_body(service)

    def _create_body(self, service: str) -> dict:
        rng = self.rng
        if service == "catalog":
            product = self.fake.product()
            product.pop("sku")
            product["stock"] = max(product["stock"], rng.randint(20, 200))
            return product
        method, card = self.fake.payment()
        if service == "payments":
            customer = rng.choice(self.customers)
            return {"order_ref": f"SUB-{rng.randint(10**6, 10**7 - 1)}", "amount": round(rng.uniform(5, 600), 2),
                    "currency": "USD", "method": method, "card": card, "customer_email": customer["email"]}
        products = self.ids["catalog"] or [rng.randint(1, 300)]
        items = [{"product_id": rng.choice(products), "qty": rng.choices([1, 2, 3, 4], [70, 20, 7, 3])[0]}
                 for _ in range(rng.choices([1, 2, 3, 4], [50, 30, 15, 5])[0])]
        return {"customer": rng.choice(self.customers), "items": items, "payment_method": method, "card": card,
                "currency": "USD", "coupon": rng.choice(["WELCOME10", "FESTIVE", None, None, None, None]),
                "shipping_method": rng.choices(["standard", "express", "same_day"], [75, 20, 5])[0]}

    def _update_body(self, service: str) -> dict:
        rng = self.rng
        if service == "catalog":
            body: dict = {"price": round(rng.uniform(5, 900), 2)}
            if rng.random() < 0.5:
                body["stock"] = rng.randint(0, 500)
            if rng.random() < 0.2:
                body["rating"] = round(rng.uniform(2.5, 5), 1)
            return body
        if service == "orders":
            return {"status": rng.choices(list(_ORDER_STATUSES), list(_ORDER_STATUSES.values()))[0]}
        return {"action": rng.choices(["capture", "refund"], [75, 25])[0]}
