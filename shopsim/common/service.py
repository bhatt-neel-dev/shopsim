"""Builds one shop service: telemetry, logging, storage, fault middleware and background work."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from shopsim import settings as settings_mod
from shopsim.common.background import Background
from shopsim.common.db import Database
from shopsim.common.downstream import Downstream
from shopsim.common.faults import FaultMiddleware
from shopsim.common.fakedata import FakeData
from shopsim.common.logs import init_logging
from shopsim.common.runtime import Runtime
from shopsim.common.telemetry import Telemetry, init_telemetry
from shopsim.settings import VERSION


class Metrics:
    def __init__(self, meter):
        self.requests = meter.create_counter("shopsim.http.requests", unit="{request}",
                                             description="Requests handled, by route and status")
        self.duration = meter.create_histogram("shopsim.http.duration", unit="ms",
                                               description="Request duration")
        self.errors = meter.create_counter("shopsim.errors", unit="{error}", description="Failed requests and jobs")
        self.injected = meter.create_counter("shopsim.faults.injected", unit="{fault}",
                                             description="Faults injected by the control panel")
        self.db_duration = meter.create_histogram("shopsim.db.duration", unit="ms", description="DB query duration")
        self.downstream_duration = meter.create_histogram("shopsim.downstream.duration", unit="ms",
                                                          description="Service-to-service call duration")
        self.log_lines = meter.create_counter("shopsim.log.lines", unit="{line}",
                                              description="Background and storm log lines emitted")


@dataclass
class ServiceContext:
    service: str
    runtime: Runtime
    telemetry: Telemetry
    logger: logging.Logger
    metrics: Metrics
    fake: FakeData
    db: Database | None = None
    downstream: dict[str, Downstream] = field(default_factory=dict)
    state: dict = field(default_factory=dict)  # per-service caches (counts for gauges, ...)

    @property
    def tracer(self):
        return self.telemetry.tracer

    @property
    def meter(self):
        return self.telemetry.meter


def create_service_app(service: str, module, transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> FastAPI:
    """``module`` provides SCHEMA, PEERS, seed(ctx), setup(ctx) -> jobs, and router(ctx)."""
    s = settings_mod.settings
    runtime = Runtime(service)
    telemetry = init_telemetry(service, runtime)
    logger = init_logging(service, runtime, telemetry)
    ctx = ServiceContext(service, runtime, telemetry, logger, Metrics(telemetry.meter), FakeData())
    ctx.db = Database(ctx, os.path.join(s.data_dir, f"{service}.db"), module.SCHEMA)
    module.seed(ctx)
    for peer in module.PEERS:
        ctx.downstream[peer] = Downstream(ctx, peer, s.service_urls[peer], (transports or {}).get(peer))
    jobs = module.setup(ctx)
    background = Background(ctx, jobs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings_mod.settings.background:
            background.start()
        logger.info("%s %s started (OTLP %s, syslog %s:%s)", telemetry.service_name, VERSION, s.otlp_endpoint,
                    s.syslog_host, s.syslog_port, extra={"fields": {"event": "service.started"}})
        yield
        logger.info("%s stopping", telemetry.service_name, extra={"fields": {"event": "service.stopping"}})
        await background.stop()
        for client in ctx.downstream.values():
            await client.aclose()
        await asyncio.to_thread(telemetry.shutdown)

    app = FastAPI(title=f"ShopSim {service}", version=VERSION, lifespan=lifespan, redoc_url=None)
    app.state.ctx = ctx
    app.state.background = background
    app.add_middleware(FaultMiddleware, ctx=ctx)
    app.include_router(module.router(ctx))

    @app.get("/health", include_in_schema=False)
    async def health():
        eff = runtime.eff
        body = {"status": "ok" if eff.state == "up" else eff.state, "service": telemetry.service_name,
                "version": VERSION, "control_connected": runtime.connected,
                "active_spikes": [sp["name"] for sp in eff.active_spikes]}
        return JSONResponse(body, status_code=200 if eff.state == "up" else 503)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=telemetry.tracer_provider,
                                       meter_provider=telemetry.meter_provider,
                                       excluded_urls="health,internal,docs,openapi.json",
                                       exclude_spans=["receive", "send"])
    return app
