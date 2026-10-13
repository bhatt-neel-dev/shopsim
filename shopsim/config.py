"""The configuration model shared by the control panel, the four services and the shoppers.

The control panel owns a ``ShopConfig`` plus a list of ``Spike``s. Each service polls the
control panel for its ``Effective`` view: the base config for that service with every active
spike folded in. Services never see spikes directly, only the numbers they should run with.
``web`` (the storefront) also gets ``Effective.frontend``: what the browser pages and the
virtual shoppers should do (sessions/min, JS errors, slow pages, ...).
"""
from __future__ import annotations

import random
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SERVICES = ("catalog", "orders", "payments", "web")
BACKENDS = ("catalog", "orders", "payments")
METHODS = ("GET", "POST", "PUT", "DELETE")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

ServiceName = Literal["catalog", "orders", "payments", "web"]
Method = Literal["GET", "POST", "PUT", "DELETE"]
Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
State = Literal["up", "degraded", "down", "flapping"]
Target = Literal["all", "random", "catalog", "orders", "payments", "web"]
SpikeKind = Literal[
    "traffic", "error_rate", "latency", "db_latency", "log_storm", "metric",
    "cpu_burn", "memory_burn", "service_down", "service_degraded",
    "rum_traffic", "js_errors", "slow_frontend", "broken_assets",
]
Journey = Literal["browse", "search", "buy", "abandon", "account"]
Device = Literal["desktop", "mobile", "tablet"]
JsError = Literal["type_error", "reference_error", "promise_rejection", "custom", "console_error"]

# Every injectable failure. `point` is where in the request it fires, so the error shows up on
# the span that would really fail (a DB lock on the DB span, a decline on the gateway span).
ERROR_TYPES: dict[str, dict] = {
    "http_500": {"status": 500, "point": "start", "methods": METHODS, "services": None,
                 "label": "Internal server error"},
    "exception": {"status": 500, "point": "logic", "methods": METHODS, "services": None,
                  "label": "Unhandled exception (real stack trace)"},
    "db_lock": {"status": 500, "point": "db", "methods": METHODS, "services": None,
                "label": "sqlite3.OperationalError: database is locked"},
    "timeout": {"status": 504, "point": "start", "methods": METHODS, "services": None,
                "label": "Cache/session read timeout"},
    "rate_limited": {"status": 429, "point": "start", "methods": METHODS, "services": None,
                     "label": "429 Too Many Requests"},
    "validation": {"status": 422, "point": "start", "methods": ("POST", "PUT"), "services": None,
                   "label": "422 request validation failed"},
    "not_found": {"status": 404, "point": "start", "methods": ("GET", "PUT", "DELETE"), "services": None,
                  "label": "404 resource not found"},
    "out_of_stock": {"status": 409, "point": "logic", "methods": ("POST",), "services": ("orders",),
                     "label": "409 item out of stock"},
    "dependency": {"status": 502, "point": "downstream", "methods": ("GET", "POST", "PUT"), "services": ("orders", "web"),
                   "label": "502 downstream connection reset"},
    "card_declined": {"status": 402, "point": "gateway", "methods": ("POST",), "services": ("payments",),
                      "label": "402 card declined by issuer"},
    "fraud_suspected": {"status": 403, "point": "gateway", "methods": ("POST",), "services": ("payments",),
                        "label": "403 blocked by fraud screen"},
    "gateway_timeout": {"status": 504, "point": "gateway", "methods": ("POST", "PUT"), "services": ("payments",),
                        "label": "504 payment gateway timeout"},
}

# Simulated resource gauges each service exports (on top of its real process metrics).
GAUGES: dict[str, dict] = {
    "cpu_pct": {"metric": "shopsim.cpu.utilization", "unit": "%", "label": "CPU %", "max": 100},
    "memory_pct": {"metric": "shopsim.memory.utilization", "unit": "%", "label": "Memory %", "max": 100},
    "disk_pct": {"metric": "shopsim.disk.utilization", "unit": "%", "label": "Disk %", "max": 100},
    "db_pool_used": {"metric": "shopsim.db.pool.used", "unit": "{connection}", "label": "DB pool in use", "max": 500},
    "queue_depth": {"metric": "shopsim.queue.depth", "unit": "{message}", "label": "Queue depth", "max": 1_000_000},
    "threads": {"metric": "shopsim.threads.active", "unit": "{thread}", "label": "Active threads", "max": 5000},
    "gc_pause_ms": {"metric": "shopsim.gc.pause", "unit": "ms", "label": "GC pause ms", "max": 60000},
    "cache_hit_pct": {"metric": "shopsim.cache.hit_ratio", "unit": "%", "label": "Cache hit %", "max": 100},
}

SPIKE_KINDS: dict[str, dict] = {
    "traffic": {"unit": "x", "default": 5, "min": 0.1, "max": 50, "label": "Traffic multiplier"},
    "error_rate": {"unit": "0-1", "default": 0.5, "min": 0, "max": 1, "label": "Error rate"},
    "latency": {"unit": "ms", "default": 1500, "min": 1, "max": 60000, "label": "Added request latency"},
    "db_latency": {"unit": "ms", "default": 800, "min": 1, "max": 60000, "label": "Added DB latency"},
    "log_storm": {"unit": "lines/s", "default": 200, "min": 1, "max": 2000, "label": "Log storm"},
    "metric": {"unit": "value", "default": 95, "min": 0, "max": 1_000_000, "label": "Metric gauge"},
    "cpu_burn": {"unit": "cores", "default": 1, "min": 0.1, "max": 4, "label": "Real CPU burn"},
    "memory_burn": {"unit": "MB", "default": 200, "min": 1, "max": 1024, "label": "Real memory burn"},
    "service_down": {"unit": None, "label": "Service down (container stopped)"},
    "service_degraded": {"unit": None, "label": "Service degraded (503s)"},
    # Storefront (browser) spikes: they always target `web` and change what the pages and the
    # virtual shoppers do, not the server.
    "rum_traffic": {"unit": "x", "default": 4, "min": 0.1, "max": 20, "label": "Shopper sessions multiplier",
                    "scope": "frontend"},
    "js_errors": {"unit": "0-1", "default": 0.6, "min": 0, "max": 1, "label": "JS error rate (per page view)",
                  "scope": "frontend"},
    "slow_frontend": {"unit": "ms", "default": 1500, "min": 50, "max": 15000,
                      "label": "Slow pages (main-thread block + late LCP)", "scope": "frontend"},
    "broken_assets": {"unit": "0-1", "default": 0.5, "min": 0, "max": 1, "label": "Broken images (404)",
                      "scope": "frontend"},
}
FRONTEND_KINDS = tuple(k for k, m in SPIKE_KINDS.items() if m.get("scope") == "frontend")

JS_ERRORS: dict[str, str] = {
    "type_error": "TypeError: Cannot read properties of undefined (a real bug in a click handler)",
    "reference_error": "ReferenceError: a function that was never shipped",
    "promise_rejection": "Unhandled promise rejection from a failed fetch",
    "custom": "Thrown CheckoutError / CartSyncError with a message",
    "console_error": "console.error() from a component",
}

DEFAULT_SERVICES: dict[str, dict] = {
    "catalog": {
        "error_rate": 0.02, "latency_ms": 35, "jitter_ms": 30, "db_latency_ms": 3, "log_rate": 4,
        "error_types": {"http_500": 2, "exception": 3, "db_lock": 3, "timeout": 1, "not_found": 1},
        "gauges": {"cpu_pct": 22, "memory_pct": 38, "disk_pct": 41, "db_pool_used": 4, "queue_depth": 0,
                   "threads": 16, "gc_pause_ms": 6, "cache_hit_pct": 87},
    },
    "orders": {
        "error_rate": 0.03, "latency_ms": 60, "jitter_ms": 50, "db_latency_ms": 5, "log_rate": 5,
        "error_types": {"http_500": 2, "exception": 3, "db_lock": 2, "timeout": 1, "validation": 2,
                        "dependency": 2, "out_of_stock": 2},
        "gauges": {"cpu_pct": 31, "memory_pct": 46, "disk_pct": 41, "db_pool_used": 7, "queue_depth": 12,
                   "threads": 24, "gc_pause_ms": 9, "cache_hit_pct": 72},
    },
    "payments": {
        "error_rate": 0.04, "latency_ms": 80, "jitter_ms": 60, "db_latency_ms": 4, "log_rate": 3,
        "error_types": {"card_declined": 5, "gateway_timeout": 2, "fraud_suspected": 1, "exception": 2,
                        "db_lock": 1, "http_500": 1},
        "gauges": {"cpu_pct": 18, "memory_pct": 33, "disk_pct": 41, "db_pool_used": 3, "queue_depth": 3,
                   "threads": 12, "gc_pause_ms": 5, "cache_hit_pct": 64},
    },
    "web": {
        "error_rate": 0.01, "latency_ms": 6, "jitter_ms": 8, "db_latency_ms": 2, "log_rate": 2,
        "error_types": {"http_500": 2, "exception": 2, "timeout": 1, "rate_limited": 1, "dependency": 3},
        "gauges": {"cpu_pct": 14, "memory_pct": 29, "disk_pct": 41, "db_pool_used": 2, "queue_depth": 0,
                   "threads": 10, "gc_pause_ms": 4, "cache_hit_pct": 91},
    },
}


def error_applies(error_type: str, service: str, method: str) -> bool:
    meta = ERROR_TYPES.get(error_type)
    if meta is None or method not in meta["methods"]:
        return False
    return meta["services"] is None or service in meta["services"]


class TrafficConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    rps: float = Field(8.0, ge=0, le=500, description="Baseline requests/sec across all services")
    # `web` traffic goes through the storefront's API (like a mobile app would); browser traffic
    # comes from the shoppers (see RumConfig).
    service_weights: dict[ServiceName, float] = Field(
        default_factory=lambda: {"catalog": 45, "orders": 35, "payments": 20, "web": 10})
    crud_ratio: dict[Method, float] = Field(
        default_factory=lambda: {"GET": 60, "POST": 20, "PUT": 15, "DELETE": 5})
    max_inflight: int = Field(200, ge=1, le=5000)
    request_timeout_s: float = Field(15, ge=1, le=120)

    @field_validator("service_weights", "crud_ratio")
    @classmethod
    def _non_negative(cls, v: dict) -> dict:
        if any(w < 0 for w in v.values()):
            raise ValueError("weights must be >= 0")
        return v


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: State = "up"
    flap_down_s: int = Field(30, ge=5, le=3600)
    flap_up_s: int = Field(90, ge=5, le=3600)
    error_rate: float = Field(0.03, ge=0, le=1)
    error_rate_by_method: dict[Method, float] = Field(default_factory=dict)
    error_types: dict[str, float] = Field(default_factory=dict)
    latency_ms: float = Field(40, ge=0, le=60000)
    jitter_ms: float = Field(40, ge=0, le=60000)
    db_latency_ms: float = Field(4, ge=0, le=60000)
    timeout_ms: float = Field(3000, ge=100, le=120000)
    log_rate: float = Field(4, ge=0, le=2000, description="Background log lines/sec")
    log_levels: dict[Level, float] = Field(
        default_factory=lambda: {"DEBUG": 15, "INFO": 62, "WARNING": 15, "ERROR": 7, "CRITICAL": 1})
    request_logs: bool = True
    gauges: dict[str, float] = Field(default_factory=dict)
    max_rows: int = Field(3000, ge=100, le=1_000_000)

    @field_validator("error_rate_by_method")
    @classmethod
    def _rates(cls, v: dict) -> dict:
        for method, rate in v.items():
            if not 0 <= rate <= 1:
                raise ValueError(f"error_rate_by_method.{method} must be between 0 and 1")
        return v

    @field_validator("error_types")
    @classmethod
    def _types(cls, v: dict) -> dict:
        for name, weight in v.items():
            if name not in ERROR_TYPES:
                raise ValueError(f"unknown error type {name!r}; known: {sorted(ERROR_TYPES)}")
            if weight < 0:
                raise ValueError("error type weights must be >= 0")
        return v

    @field_validator("log_levels")
    @classmethod
    def _levels(cls, v: dict) -> dict:
        if any(w < 0 for w in v.values()):
            raise ValueError("log level weights must be >= 0")
        return v

    @field_validator("gauges")
    @classmethod
    def _gauges(cls, v: dict) -> dict:
        for name, value in v.items():
            if name not in GAUGES:
                raise ValueError(f"unknown gauge {name!r}; known: {sorted(GAUGES)}")
            if not 0 <= value <= GAUGES[name]["max"]:
                raise ValueError(f"gauge {name} must be between 0 and {GAUGES[name]['max']}")
        return v


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    traces: bool = True
    metrics: bool = True
    logs_syslog: bool = True
    logs_file: bool = True
    logs_stdout: bool = True
    logs_otlp: bool = False
    # Each log line written while handling a request is also added to the active span as a
    # `log` event, so the trace view shows the logs that belong to it.
    log_span_events: bool = True
    sample_ratio: float = Field(1.0, ge=0, le=1)
    # What goes over syslog, so a log backend is not swamped. Files and stdout always get
    # everything. "text" is one short line (message + event + trace/span ids) that pattern
    # detection can cluster; "json" is the full document. The defaults are deliberately gentle:
    # full JSON with stack traces at every level made Motadata's log-pattern view run out of memory.
    syslog_format: Literal["json", "text"] = "text"
    syslog_min_level: Level = "INFO"
    syslog_max_per_s: float = Field(25, ge=0, le=5000, description="Per service; 0 = no cap")
    syslog_stack_traces: bool = False


def _weights(v: dict, what: str) -> dict:
    if any(w < 0 for w in v.values()):
        raise ValueError(f"{what} weights must be >= 0")
    return v


class RumConfig(BaseModel):
    """Real-user monitoring: headless browsers (the shoppers) run the storefront with the
    Motadata RUM SDK. Everything here is what those pages and shoppers do."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    sessions_per_min: float = Field(6, ge=0, le=120, description="New shopper sessions per minute")
    max_concurrent: int = Field(2, ge=1, le=8, description="Browser sessions at once (also capped by "
                                                           "SHOPSIM_MAX_SHOPPERS)")
    journeys: dict[Journey, float] = Field(
        default_factory=lambda: {"browse": 30, "search": 20, "buy": 30, "abandon": 15, "account": 5})
    devices: dict[Device, float] = Field(default_factory=lambda: {"desktop": 55, "mobile": 35, "tablet": 10})
    think_time_ms: float = Field(1500, ge=0, le=60000, description="Mean pause between a shopper's steps")
    js_error_rate: float = Field(0.05, ge=0, le=1, description="Chance per page view of a JS error")
    js_error_types: dict[JsError, float] = Field(default_factory=lambda: {
        "type_error": 4, "reference_error": 2, "promise_rejection": 2, "custom": 2, "console_error": 2})
    slow_render_ms: float = Field(0, ge=0, le=15000, description="Main-thread block (long task) per slow view")
    slow_render_rate: float = Field(0.1, ge=0, le=1, description="Share of views that get the long task")
    lcp_delay_ms: float = Field(0, ge=0, le=20000, description="Extra delay on the hero/product image")
    layout_shift_rate: float = Field(0.1, ge=0, le=1, description="Chance of a late promo banner (CLS)")
    broken_asset_rate: float = Field(0.02, ge=0, le=1, description="Chance a product image 404s")
    rage_click_rate: float = Field(0.05, ge=0, le=1, description="Chance a session rage-clicks a dead button")
    api_latency_ms: float = Field(0, ge=0, le=30000, description="Extra latency on the storefront's API")
    geo_spoof: bool = Field(True, description="Give each session a public IP (X-Forwarded-For) for geo data")

    @field_validator("journeys", "devices", "js_error_types")
    @classmethod
    def _non_negative(cls, v: dict) -> dict:
        return _weights(v, "mix")


class ShopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    traffic: TrafficConfig = Field(default_factory=TrafficConfig)
    services: dict[ServiceName, ServiceConfig]
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    rum: RumConfig = Field(default_factory=RumConfig)

    @model_validator(mode="before")
    @classmethod
    def _fill_new_services(cls, data):
        """Configs saved before a service existed (web) get that service's defaults."""
        if isinstance(data, dict) and isinstance(data.get("services"), dict):
            services = dict(data["services"])
            for name in SERVICES:
                services.setdefault(name, DEFAULT_SERVICES[name])
            data = {**data, "services": services}
        return data

    @model_validator(mode="after")
    def _all_services(self) -> "ShopConfig":
        missing = [s for s in SERVICES if s not in self.services]
        if missing:
            raise ValueError(f"services missing: {missing}")
        return self


def default_service_config(service: str) -> ServiceConfig:
    return ServiceConfig(**DEFAULT_SERVICES[service])


def default_config() -> ShopConfig:
    return ShopConfig(services={s: default_service_config(s) for s in SERVICES})


class SpikeSpec(BaseModel):
    """What a user asks for: a kind, a target, a size, a duration and when it should run."""
    model_config = ConfigDict(extra="forbid")

    kind: SpikeKind
    target: Target = "all"
    value: float | None = None
    metric: str | None = None
    level: Level | None = None
    error_type: str | None = None
    duration_s: int = Field(120, ge=5, le=86400)
    start_in_s: float = Field(0, ge=0, le=30 * 86400)
    start_at: float | None = Field(None, description="Epoch seconds; overrides start_in_s")
    repeat_every_s: int | None = Field(None, ge=10, le=30 * 86400)
    name: str | None = Field(None, max_length=80)

    @model_validator(mode="after")
    def _check(self) -> "SpikeSpec":
        meta = SPIKE_KINDS[self.kind]
        if self.kind == "metric":
            if self.metric not in GAUGES:
                raise ValueError(f"metric spike needs metric= one of {sorted(GAUGES)}")
            if self.value is None:
                self.value = min(meta["default"], GAUGES[self.metric]["max"])
            if not 0 <= self.value <= GAUGES[self.metric]["max"]:
                raise ValueError(f"{self.metric} must be between 0 and {GAUGES[self.metric]['max']}")
        elif meta["unit"] is None:
            self.value = None
        else:
            if self.value is None:
                self.value = meta["default"]
            if not meta["min"] <= self.value <= meta["max"]:
                raise ValueError(f"{self.kind} value must be between {meta['min']} and {meta['max']} {meta['unit']}")
        if self.kind == "log_storm" and self.level is None:
            self.level = "ERROR"
        if self.kind in FRONTEND_KINDS:
            if self.target not in ("web", "all"):
                raise ValueError(f"{self.kind} is a storefront spike; its target is web")
            self.target = "web"
        if self.error_type is not None:
            if self.kind != "error_rate":
                raise ValueError("error_type only applies to error_rate spikes")
            if self.error_type not in ERROR_TYPES:
                raise ValueError(f"unknown error type {self.error_type!r}")
        if self.repeat_every_s is not None and self.repeat_every_s < self.duration_s:
            raise ValueError("repeat_every_s must be >= duration_s")
        return self

    def describe(self) -> str:
        if self.name:
            return self.name
        label = SPIKE_KINDS[self.kind]["label"]
        if self.kind == "metric":
            return f"{GAUGES[self.metric]['label']} -> {self.value:g}"
        if self.value is None:
            return label
        unit = SPIKE_KINDS[self.kind]["unit"]
        return f"{label} {self.value:g}{'' if unit in ('x',) else ' '}{unit}"


class Spike(SpikeSpec):
    model_config = ConfigDict(extra="ignore")

    id: str
    status: Literal["scheduled", "active", "done", "cancelled"] = "scheduled"
    created_at: float
    next_start: float | None = None
    started_at: float | None = None
    ends_at: float | None = None
    runs: int = 0
    resolved_target: str | None = None

    def targets(self) -> tuple[str, ...]:
        if self.kind in FRONTEND_KINDS:
            return ("web",)
        if self.target == "all":
            return SERVICES
        if self.target == "random":
            return (self.resolved_target,) if self.resolved_target else ()
        return (self.target,)

    def activate(self, now: float) -> None:
        self.status = "active"
        self.started_at = now
        self.ends_at = now + self.duration_s
        self.runs += 1
        if self.target == "random":
            self.resolved_target = random.choice(SERVICES)


class Effective(BaseModel):
    """What one service should do right now: its base config with active spikes applied."""
    service: str
    state: Literal["up", "degraded", "down"] = "up"
    down_mode: Literal["container", "simulated"] = "container"
    error_rate: float = 0.0
    error_rate_by_method: dict[str, float] = Field(default_factory=dict)
    error_types: dict[str, float] = Field(default_factory=dict)
    forced_error_type: str | None = None
    latency_ms: float = 0
    jitter_ms: float = 0
    db_latency_ms: float = 0
    timeout_ms: float = 3000
    log_rate: float = 0
    log_levels: dict[str, float] = Field(default_factory=dict)
    log_storms: list[dict] = Field(default_factory=list)
    request_logs: bool = True
    gauges: dict[str, float] = Field(default_factory=dict)
    spiked_gauges: list[str] = Field(default_factory=list)
    cpu_burn_cores: float = 0
    memory_burn_mb: float = 0
    traffic_multiplier: float = 1.0
    max_rows: int = 3000
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    active_spikes: list[dict] = Field(default_factory=list)
    version: int = 0
    frontend: "FrontendEffective | None" = None


class FrontendEffective(BaseModel):
    """What the storefront pages and the shoppers should do right now (RumConfig + spikes)."""
    enabled: bool = True
    sessions_per_min: float = 0
    max_concurrent: int = 2
    journeys: dict[str, float] = Field(default_factory=dict)
    devices: dict[str, float] = Field(default_factory=dict)
    think_time_ms: float = 1500
    js_error_rate: float = 0
    js_error_types: dict[str, float] = Field(default_factory=dict)
    slow_render_ms: float = 0
    slow_render_rate: float = 0
    lcp_delay_ms: float = 0
    layout_shift_rate: float = 0
    broken_asset_rate: float = 0
    rage_click_rate: float = 0
    api_latency_ms: float = 0
    geo_spoof: bool = True
    web_state: str = "up"
    active_spikes: list[dict] = Field(default_factory=list)


def compute_frontend(cfg: ShopConfig, spikes: list[Spike], web_state: str = "up") -> FrontendEffective:
    r = cfg.rum
    fe = FrontendEffective(**r.model_dump(exclude={"enabled"}), enabled=r.enabled, web_state=web_state)
    for sp in spikes:
        if sp.status != "active" or "web" not in sp.targets():
            continue
        if sp.kind == "rum_traffic":
            fe.sessions_per_min *= sp.value
        elif sp.kind == "traffic":  # a traffic spike on the shop brings more shoppers too
            fe.sessions_per_min *= sp.value
        elif sp.kind == "js_errors":
            fe.js_error_rate = max(fe.js_error_rate, sp.value)
        elif sp.kind == "slow_frontend":
            fe.slow_render_ms += sp.value
            fe.lcp_delay_ms += sp.value
            fe.slow_render_rate = max(fe.slow_render_rate, 0.8)
        elif sp.kind == "broken_assets":
            fe.broken_asset_rate = max(fe.broken_asset_rate, sp.value)
        else:
            continue
        fe.active_spikes.append({"id": sp.id, "kind": sp.kind, "value": sp.value, "name": sp.describe(),
                                 "ends_at": sp.ends_at})
    fe.sessions_per_min = min(fe.sessions_per_min, 600)
    return fe


Effective.model_rebuild()

def compute_effective(cfg: ShopConfig, spikes: list[Spike], service: str, *, now: float | None = None,
                      flap_since: float | None = None, down_mode: str = "container",
                      version: int = 0) -> Effective:
    now = time.time() if now is None else now
    s = cfg.services[service]
    state = s.state
    if state == "flapping":
        cycle = s.flap_up_s + s.flap_down_s
        phase = (now - (flap_since if flap_since is not None else now)) % cycle
        state = "up" if phase < s.flap_up_s else "down"

    eff = Effective(
        service=service, down_mode=down_mode, error_rate=s.error_rate,
        error_rate_by_method=dict(s.error_rate_by_method), error_types=dict(s.error_types),
        latency_ms=s.latency_ms, jitter_ms=s.jitter_ms, db_latency_ms=s.db_latency_ms,
        timeout_ms=s.timeout_ms, log_rate=s.log_rate, log_levels=dict(s.log_levels),
        request_logs=s.request_logs, gauges=dict(s.gauges), max_rows=s.max_rows,
        telemetry=cfg.telemetry.model_copy(), version=version,
    )
    forced_down = forced_degraded = False
    for sp in spikes:
        if sp.status != "active" or service not in sp.targets():
            continue
        kind = sp.kind
        if kind == "error_rate":
            eff.error_rate = max(eff.error_rate, sp.value)
            eff.error_rate_by_method = {m: max(r, sp.value) for m, r in eff.error_rate_by_method.items()}
            if sp.error_type:
                # Applied only on requests it fits (a card decline needs a POST); others fall back
                # to the service's own error mix. See faults.pick_error.
                eff.forced_error_type = sp.error_type
        elif kind == "latency":
            eff.latency_ms += sp.value
        elif kind == "db_latency":
            eff.db_latency_ms += sp.value
        elif kind == "log_storm":
            eff.log_storms.append({"rate": sp.value, "level": sp.level})
        elif kind == "metric":
            eff.gauges[sp.metric] = sp.value
            eff.spiked_gauges.append(sp.metric)
        elif kind == "cpu_burn":
            eff.cpu_burn_cores += sp.value
        elif kind == "memory_burn":
            eff.memory_burn_mb += sp.value
        elif kind == "traffic":
            eff.traffic_multiplier *= sp.value
        elif kind == "service_down":
            forced_down = True
        elif kind == "service_degraded":
            forced_degraded = True
        eff.active_spikes.append({"id": sp.id, "kind": kind, "value": sp.value, "name": sp.describe(),
                                  "ends_at": sp.ends_at})
    if forced_down:
        state = "down"
    elif forced_degraded and state == "up":
        state = "degraded"
    eff.state = state
    if service == "web":
        eff.frontend = compute_frontend(cfg, spikes, state)
    return eff
