"""Control panel: web UI + REST API. Services poll ``/api/effective/{service}``; everything else is
for people (the UI) and automation (curl, scripts)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from shopsim import settings as settings_mod
from shopsim.config import (
    ERROR_TYPES,
    GAUGES,
    JS_ERRORS,
    LEVELS,
    METHODS,
    SERVICES,
    SPIKE_KINDS,
    SpikeSpec,
    State,
)
from shopsim.control.docker_ctl import DockerControl
from shopsim.control.presets import PRESETS
from shopsim.control.store import Store
from shopsim.control.traffic import TrafficGenerator
from shopsim.settings import VERSION

STATIC = Path(__file__).parent / "static"


class StateIn(BaseModel):
    state: State
    flap_down_s: int | None = Field(None, ge=5, le=3600)
    flap_up_s: int | None = Field(None, ge=5, le=3600)


def _invalid(exc: ValidationError) -> HTTPException:
    return HTTPException(422, detail=json.loads(exc.json(include_url=False)))


def _logger() -> logging.Logger:
    log = logging.getLogger("shopsim.control")
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s shopsim-control %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def create_app(transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> FastAPI:
    s = settings_mod.settings
    log = _logger()
    store = Store(Path(s.data_dir) / "control.json")
    store.on_event = lambda e: log.log(
        {"error": logging.ERROR, "warning": logging.WARNING}.get(e["level"], logging.INFO), e["message"])
    docker = DockerControl(s.docker_socket, s.container_prefix)
    traffic = TrafficGenerator(store, s.service_urls, s.max_total_rps, transports)
    health = {svc: {"status": "unknown", "detail": "", "checked_at": None} for svc in SERVICES}
    health_client = httpx.AsyncClient(timeout=1.5)

    async def scheduler():
        while True:
            store.tick()
            await asyncio.sleep(1)

    async def reconciler():
        await docker.check()
        store.event("info" if docker.available else "warning",
                    "Container control: " + ("enabled, 'down' stops the container" if docker.available else
                                             f"unavailable ({docker.reason}); 'down' is simulated (503 + no telemetry)"))
        while True:
            try:
                await docker.refresh()
                store.down_mode = "container" if docker.available else "simulated"
                if docker.available:
                    for svc in SERVICES:
                        want_down = store.effective(svc).state == "down"
                        actual = docker.states.get(svc)
                        if want_down and actual == "running":
                            await docker.stop(svc)
                            store.event("warning", f"{svc}: container stopped (service down)", service=svc)
                        elif not want_down and actual in ("exited", "created"):
                            await docker.start(svc)
                            store.event("info", f"{svc}: container started (service up)", service=svc)
            except Exception as exc:  # noqa: BLE001
                store.event("error", f"Container reconcile failed: {exc}")
            await asyncio.sleep(2)

    async def health_poller():
        while True:
            for svc in SERVICES:
                url = f"{s.service_urls[svc]}/health"
                try:
                    resp = await health_client.get(url)
                    body = resp.json()
                    status = "ok" if resp.status_code == 200 else body.get("status", f"http {resp.status_code}")
                    health[svc] = {"status": status, "detail": body, "checked_at": time.time()}
                except Exception as exc:  # noqa: BLE001
                    health[svc] = {"status": "unreachable", "detail": type(exc).__name__, "checked_at": time.time()}
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if settings_mod.settings.background:
            loop = asyncio.get_running_loop()
            tasks = [loop.create_task(fn()) for fn in (scheduler, reconciler, health_poller, traffic.run)]
        store.event("info", f"Control panel {VERSION} started; traffic "
                            f"{'on' if store.config.traffic.enabled else 'off'} at {store.config.traffic.rps} rps")
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await traffic.aclose()
        await docker.aclose()
        await health_client.aclose()

    app = FastAPI(title="ShopSim control panel", version=VERSION, lifespan=lifespan, redoc_url=None)
    app.state.store = store
    app.state.traffic = traffic
    app.state.docker = docker

    if s.control_password:
        expected = "Basic " + base64.b64encode(f"admin:{s.control_password}".encode()).decode()

        @app.middleware("http")
        async def basic_auth(request, call_next):
            path = request.url.path
            if path.startswith("/api/effective/") or path == "/healthz" or secrets.compare_digest(
                    request.headers.get("authorization", ""), expected):
                return await call_next(request)
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="shopsim"'})

    # ---- pages -----------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "version": VERSION}

    # ---- for the services ------------------------------------------------------------------------
    @app.get("/api/effective/{service}")
    async def effective(service: str):
        if service not in SERVICES:
            raise HTTPException(404, f"unknown service {service!r}")
        return store.effective(service).model_dump(mode="json")

    # ---- config ----------------------------------------------------------------------------------
    @app.get("/api/config")
    async def get_config():
        return store.config.model_dump(mode="json")

    @app.patch("/api/config")
    async def patch_config(patch: dict):
        try:
            return store.patch_config(patch).model_dump(mode="json")
        except ValidationError as exc:
            raise _invalid(exc) from exc

    @app.put("/api/config")
    async def put_config(data: dict):
        try:
            return store.replace_config(data).model_dump(mode="json")
        except ValidationError as exc:
            raise _invalid(exc) from exc

    @app.post("/api/config/reset")
    async def reset_config():
        return store.reset_config().model_dump(mode="json")

    @app.post("/api/rum/report", include_in_schema=False)
    async def rum_report(report: dict):
        """The shoppers post their counters here every few seconds."""
        store.rum_report = {**report, "received_at": time.time()}
        return {"ok": True}

    @app.post("/api/services/{service}/state")
    async def set_state(service: str, body: StateIn):
        if service not in SERVICES:
            raise HTTPException(404, f"unknown service {service!r}")
        store.set_state(service, body.state, body.flap_down_s, body.flap_up_s)
        return store.effective(service).model_dump(mode="json")

    # ---- spikes ----------------------------------------------------------------------------------
    @app.get("/api/spikes")
    async def list_spikes():
        return _spikes(store)

    @app.post("/api/spikes", status_code=201)
    async def add_spike(spec: SpikeSpec):
        return store.add_spike(spec).model_dump(mode="json")

    @app.delete("/api/spikes/{spike_id}")
    async def cancel_spike(spike_id: str):
        if spike_id not in store.spikes:
            raise HTTPException(404, f"no spike {spike_id}")
        return store.cancel_spike(spike_id).model_dump(mode="json")

    @app.post("/api/spikes/cancel-all")
    async def cancel_all():
        return {"cancelled": store.cancel_all()}

    # ---- presets ---------------------------------------------------------------------------------
    @app.get("/api/presets")
    async def presets():
        return [{"name": name, "description": desc} for name, (desc, _) in PRESETS.items()]

    @app.post("/api/presets/{name}")
    async def apply_preset(name: str):
        if name not in PRESETS:
            raise HTTPException(404, f"unknown preset {name!r}; known: {sorted(PRESETS)}")
        store.event("info", f"Preset applied: {name}")
        PRESETS[name][1](store)
        return {"applied": name, "spikes": _spikes(store)}

    # ---- views -----------------------------------------------------------------------------------
    @app.get("/api/catalogue")
    async def catalogue():
        return {"services": SERVICES, "methods": METHODS, "levels": LEVELS,
                "states": ["up", "degraded", "down", "flapping"], "error_types": ERROR_TYPES, "gauges": GAUGES,
                "spike_kinds": SPIKE_KINDS, "js_errors": JS_ERRORS,
                "journeys": ["browse", "search", "buy", "abandon", "account"],
                "devices": ["desktop", "mobile", "tablet"],
                "presets": [{"name": n, "description": d} for n, (d, _) in PRESETS.items()]}

    @app.get("/api/status")
    async def status():
        now = time.time()
        rates = traffic.rates()
        services = {}
        for svc in SERVICES:
            eff = store.effective(svc, now)
            services[svc] = {
                "health": health[svc]["status"], "container": docker.states.get(svc, "unknown"),
                "effective": {k: getattr(eff, k) for k in (
                    "state", "error_rate", "forced_error_type", "latency_ms", "db_latency_ms", "log_rate",
                    "log_storms", "cpu_burn_cores", "memory_burn_mb", "traffic_multiplier", "spiked_gauges",
                    "active_spikes")},
                "target_rps": round(rates[svc], 2),
                "stats": traffic.stats.snapshot(svc, now),
                "series": list(traffic.stats.series[svc]),
            }
        report = store.rum_report or {}
        web_detail = health["web"]["detail"] if isinstance(health["web"]["detail"], dict) else {}
        rum = {"effective": store.effective("web", now).frontend.model_dump(mode="json"),
               "shoppers": report, "shoppers_age_s": round(now - report["received_at"], 1) if report else None,
               "relay": web_detail.get("rum", {})}
        return {
            "now": now, "version": store.version, "config": store.config.model_dump(mode="json"),
            "spikes": _spikes(store), "services": services, "rum": rum,
            "traffic": {"target_rps": round(sum(rates.values()), 2), "inflight": traffic.inflight,
                        "dropped": traffic.stats.dropped, "max_total_rps": s.max_total_rps},
            "events": list(store.events)[:60],
            "meta": {"version": VERSION, "otlp_endpoint": s.otlp_endpoint,
                     "otlp_traces_endpoint": s.otlp_traces_endpoint,
                     "syslog": f"{s.syslog_host}:{s.syslog_port}", "log_dir": s.log_dir,
                     "environment": s.environment, "hostname": s.hostname,
                     "docker": docker.available, "docker_reason": docker.reason, "down_mode": store.down_mode,
                     "max_burn_cores": s.max_burn_cores, "max_burn_mb": s.max_burn_mb},
        }

    @app.get("/api/data/{service}")
    async def data(service: str, limit: int = 15):
        if service not in SERVICES:
            raise HTTPException(404, f"unknown service {service!r}")
        try:
            resp = await traffic.clients[service].get("/internal/recent", params={"limit": min(limit, 100)},
                                                      timeout=3)
            return resp.json()
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"{service} unreachable: {type(exc).__name__}") from exc

    return app


def _spikes(store: Store) -> list[dict]:
    order = {"active": 0, "scheduled": 1, "done": 2, "cancelled": 3}
    spikes = sorted(store.spikes.values(), key=lambda sp: (order[sp.status], -(sp.created_at or 0)))
    return [{**sp.model_dump(mode="json"), "label": sp.describe()} for sp in spikes]
