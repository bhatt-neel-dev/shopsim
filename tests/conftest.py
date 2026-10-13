import os
import tempfile

import httpx
import pytest

_TMP = tempfile.mkdtemp(prefix="shopsim-tests-")
os.environ.update({
    "SHOPSIM_EXPORTERS": "memory",
    "SHOPSIM_DATA_DIR": os.path.join(_TMP, "data"),
    "SHOPSIM_LOG_DIR": os.path.join(_TMP, "logs"),
    "SHOPSIM_BACKGROUND": "false",
    "SHOPSIM_SYSLOG_HOST": "127.0.0.1",
    "SHOPSIM_SYSLOG_PORT": "9",  # discard
    "SHOPSIM_DOCKER_SOCKET": "/nonexistent",
})

from shopsim import settings as settings_mod  # noqa: E402

settings_mod.reload()


def quiet(app) -> None:
    """No latency and no random faults unless a test asks for them."""
    eff = app.state.ctx.runtime.eff
    eff.error_rate = 0
    eff.latency_ms = eff.jitter_ms = eff.db_latency_ms = 0
    eff.telemetry.logs_stdout = False


@pytest.fixture(scope="session")
def shop():
    """All three services wired together in-process (orders calls the others via ASGI)."""
    from shopsim.services import create

    catalog = create("catalog")
    payments = create("payments")
    orders = create("orders", transports={"catalog": httpx.ASGITransport(app=catalog),
                                          "payments": httpx.ASGITransport(app=payments)})
    apps = {"catalog": catalog, "orders": orders, "payments": payments}
    for app in apps.values():
        quiet(app)
    return apps


@pytest.fixture(autouse=True)
def _reset(shop):
    for app in shop.values():
        quiet(app)
        eff = app.state.ctx.runtime.eff
        eff.state = "up"
        eff.forced_error_type = None
        eff.error_rate_by_method = {}
    yield


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
