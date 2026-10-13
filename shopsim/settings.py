"""Process settings from the environment. Runtime behaviour lives in the control panel, not here."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field

VERSION = "0.2.0"
PORTS = {"control": 8180, "catalog": 8181, "orders": 8182, "payments": 8183, "web": 8184}


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    bind: str = field(default_factory=lambda: _env("SHOPSIM_BIND", "0.0.0.0"))
    control_url: str = field(default_factory=lambda: _env("SHOPSIM_CONTROL_URL", "http://127.0.0.1:8180"))
    service_urls: dict = field(default_factory=lambda: {
        "catalog": _env("SHOPSIM_CATALOG_URL", "http://127.0.0.1:8181"),
        "orders": _env("SHOPSIM_ORDERS_URL", "http://127.0.0.1:8182"),
        "payments": _env("SHOPSIM_PAYMENTS_URL", "http://127.0.0.1:8183"),
        "web": _env("SHOPSIM_WEB_URL", "http://127.0.0.1:8184"),
    })
    otlp_endpoint: str = field(default_factory=lambda: _env("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318"))
    # Full URL (OTel convention). Lets traces go to a different collector than metrics/logs.
    otlp_traces_endpoint: str = field(default_factory=lambda: _env("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""))
    # otlp | memory (tests) | none
    exporters: str = field(default_factory=lambda: _env("SHOPSIM_EXPORTERS", "otlp"))
    metric_interval_s: float = field(default_factory=lambda: float(_env("SHOPSIM_METRIC_INTERVAL_S", "10")))
    syslog_host: str = field(default_factory=lambda: _env("SHOPSIM_SYSLOG_HOST", "127.0.0.1"))
    syslog_port: int = field(default_factory=lambda: int(_env("SHOPSIM_SYSLOG_PORT", "514")))
    log_dir: str = field(default_factory=lambda: _env("SHOPSIM_LOG_DIR", "/var/log/shopsim"))
    data_dir: str = field(default_factory=lambda: _env("SHOPSIM_DATA_DIR", "/data"))
    environment: str = field(default_factory=lambda: _env("SHOPSIM_ENV", "lab"))
    hostname: str = field(default_factory=lambda: _env("SHOPSIM_HOSTNAME", socket.gethostname()))
    docker_socket: str = field(default_factory=lambda: _env("SHOPSIM_DOCKER_SOCKET", "/var/run/docker.sock"))
    container_prefix: str = field(default_factory=lambda: _env("SHOPSIM_CONTAINER_PREFIX", "shopsim-"))
    control_password: str = field(default_factory=lambda: _env("SHOPSIM_CONTROL_PASSWORD", ""))
    # Safety caps: this usually runs next to other things (here: the Motadata appliance itself).
    max_total_rps: float = field(default_factory=lambda: float(_env("SHOPSIM_MAX_TOTAL_RPS", "600")))
    max_burn_cores: float = field(default_factory=lambda: float(_env("SHOPSIM_MAX_BURN_CORES", "2")))
    max_burn_mb: float = field(default_factory=lambda: float(_env("SHOPSIM_MAX_BURN_MB", "300")))
    poll_interval_s: float = field(default_factory=lambda: float(_env("SHOPSIM_POLL_INTERVAL_S", "2")))
    # Storefront RUM: the values from the RUM application registered in Motadata. Without an
    # intake URL the storefront still works, just without the RUM SDK.
    rum_intake_url: str = field(default_factory=lambda: _env("RUM_INTAKE_URL", ""))
    rum_application_id: str = field(default_factory=lambda: _env("RUM_APPLICATION_ID", ""))
    rum_client_token: str = field(default_factory=lambda: _env("RUM_CLIENT_TOKEN", ""))
    rum_service: str = field(default_factory=lambda: _env("RUM_SERVICE", "ShopSim Storefront"))
    rum_version: str = field(default_factory=lambda: _env("RUM_VERSION", VERSION))
    rum_verify_tls: bool = field(default_factory=lambda: _env("RUM_VERIFY_TLS", "false").lower() == "true")
    # Shoppers: where they browse, and a hard cap on browsers at once (memory).
    storefront_url: str = field(default_factory=lambda: _env("SHOPSIM_STOREFRONT_URL", "http://127.0.0.1:8184"))
    max_shoppers: int = field(default_factory=lambda: int(_env("SHOPSIM_MAX_SHOPPERS", "3")))
    background: bool = field(default_factory=lambda: _env("SHOPSIM_BACKGROUND", "true").lower() == "true")

    def __post_init__(self):
        if not self.otlp_traces_endpoint:
            self.otlp_traces_endpoint = f"{self.otlp_endpoint.rstrip('/')}/v1/traces"


settings = Settings()


def reload() -> Settings:
    """Re-read the environment (tests change it between apps)."""
    global settings
    settings = Settings()
    return settings
