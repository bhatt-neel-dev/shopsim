import logging
import time

from shopsim.common.logs import SyslogFormatter, SyslogGate
from shopsim.config import compute_effective, default_config


class FakeRuntime:
    def __init__(self, **telemetry):
        self.eff = compute_effective(default_config(), [], "orders")
        for key, value in telemetry.items():
            setattr(self.eff.telemetry, key, value)


def record(level=logging.INFO, msg="GET /orders 200 12.5ms", fields=None, exc=False):
    rec = logging.LogRecord("shopsim.orders", level, __file__, 1, msg, (), None)
    rec.trace_id, rec.span_id = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"
    rec.fields = fields or {"event": "http.access", "http.response.status_code": 200}
    if exc:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            rec.exc_info = sys.exc_info()
    return rec


def test_defaults_are_gentle():
    t = default_config().telemetry
    assert (t.syslog_format, t.syslog_min_level, t.syslog_max_per_s, t.syslog_stack_traces) == ("text", "INFO", 25, False)


def test_text_format_is_one_short_line_with_the_trace_ids():
    line = SyslogFormatter("shopsim-orders", FakeRuntime()).format(record())
    body = line.split(": ", 1)[1]
    assert body == ("INFO GET /orders 200 12.5ms | event=http.access trace_id=0af7651916cd43dd8448eb211c80319c "
                    "span_id=b7ad6b7169203331 http.response.status_code=200")
    err = SyslogFormatter("shopsim-orders", FakeRuntime()).format(record(logging.ERROR, "failed", exc=True))
    assert "error.type=ValueError" in err and "Traceback" not in err and "\n" not in err


def test_json_format_keeps_stack_traces_only_when_asked():
    with_stack = SyslogFormatter("shopsim-orders", FakeRuntime(syslog_format="json", syslog_stack_traces=True))
    without = SyslogFormatter("shopsim-orders", FakeRuntime(syslog_format="json"))
    assert "error.stack_trace" in with_stack.format(record(logging.ERROR, exc=True))
    assert "error.stack_trace" not in without.format(record(logging.ERROR, exc=True))


def test_gate_drops_low_levels_and_caps_the_rate():
    gate = SyslogGate(FakeRuntime(syslog_min_level="WARNING", syslog_max_per_s=0))
    assert not gate.filter(record(logging.INFO)) and gate.filter(record(logging.ERROR))

    gate = SyslogGate(FakeRuntime(syslog_min_level="DEBUG", syslog_max_per_s=5))
    gate.tokens, gate.last = 5, time.monotonic()
    passed = sum(gate.filter(record()) for _ in range(50))
    assert 5 <= passed <= 6 and gate.dropped == 50 - passed


async def test_normal_preset_keeps_telemetry_settings(shop):
    from conftest import client
    from shopsim.control.app import create_app

    app = create_app()
    async with client(app) as c:
        await c.patch("/api/config", json={"telemetry": {"syslog_format": "json", "syslog_max_per_s": 3},
                                           "traffic": {"rps": 99}})
        await c.post("/api/presets/normal")
        cfg = (await c.get("/api/config")).json()
    assert cfg["telemetry"]["syslog_format"] == "json" and cfg["telemetry"]["syslog_max_per_s"] == 3
    assert cfg["traffic"]["rps"] != 99
