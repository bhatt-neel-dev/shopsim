"""Structured JSON logs with trace correlation, shipped to stdout, a rotating file, syslog and
(optionally) OTLP, and attached to the active span as events. Each destination can be switched on
and off at runtime from the control panel.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler, SysLogHandler

from opentelemetry import trace

from shopsim import settings as settings_mod

logging.raiseExceptions = False  # an unreachable syslog target must never break a request

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PRIMITIVE = (str, int, float, bool)


class TraceContextFilter(logging.Filter):
    """Stamp the current (sampled) trace and span ids on every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid and ctx.trace_flags.sampled:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id = format(ctx.span_id, "016x")
        else:
            record.trace_id = record.span_id = ""
        return True


class MuteFilter(logging.Filter):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def filter(self, record: logging.LogRecord) -> bool:
        return not self.runtime.muted


class ToggleFilter(logging.Filter):
    """Pass records only while ``telemetry.<flag>`` is on in the live config."""

    def __init__(self, runtime, flag: str):
        super().__init__()
        self.runtime = runtime
        self.flag = flag

    def filter(self, record: logging.LogRecord) -> bool:
        return bool(getattr(self.runtime.eff.telemetry, self.flag))


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str, max_stack: int = 16_000):
        super().__init__()
        self.service_name = service_name
        self.max_stack = max_stack
        s = settings_mod.settings
        self.host = s.hostname
        self.env = s.environment

    def to_dict(self, record: logging.LogRecord) -> dict:
        doc = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", ""),
            "span_id": getattr(record, "span_id", ""),
            "host": self.host,
            "env": self.env,
            "pid": record.process,
        }
        fields = getattr(record, "fields", None)
        if fields:
            doc.update(fields)
        if record.exc_info and record.exc_info[1] is not None:
            etype, evalue, tb = record.exc_info
            doc["error.type"] = etype.__name__
            doc["error.message"] = str(evalue)
            doc["error.stack_trace"] = "".join(traceback.format_exception(etype, evalue, tb))[-self.max_stack:]
        return doc

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.to_dict(record), default=str, separators=(",", ":"))


class SyslogFormatter(JsonFormatter):
    """RFC 3164 header + JSON body. SysLogHandler adds the <PRI> prefix itself."""

    def __init__(self, service_name: str):
        super().__init__(service_name, max_stack=6000)

    def format(self, record: logging.LogRecord) -> str:
        t = time.localtime(record.created)
        stamp = f"{_MONTHS[t.tm_mon - 1]} {t.tm_mday:>2} {t.tm_hour:02}:{t.tm_min:02}:{t.tm_sec:02}"
        return f"{stamp} {self.host} {self.service_name}[{record.process}]: {super().format(record)}"


class SpanEventHandler(logging.Handler):
    """Adds each log line to the span it was written under, as a ``log`` event.

    Trace backends show span events inside the trace, so the logs of a request sit next to its
    spans. The stack trace is left out: a failing span already carries it as an exception event.
    """

    def emit(self, record: logging.LogRecord) -> None:
        span = trace.get_current_span()
        if not span.is_recording():
            return
        try:
            attrs = {"log.severity": record.levelname, "log.message": record.getMessage(),
                     "log.logger": record.name}
            for key, value in (getattr(record, "fields", None) or {}).items():
                if value is not None:
                    attrs[f"log.{key}"] = value if isinstance(value, _PRIMITIVE) else str(value)
            if record.exc_info and record.exc_info[1] is not None:
                attrs["log.error.type"] = record.exc_info[0].__name__
            span.add_event("log", attrs)
        except Exception:  # noqa: BLE001 - logging must never break the request
            self.handleError(record)


def _otlp_handler(logger_provider, runtime):
    from opentelemetry.sdk._logs import LoggingHandler

    class FlatteningHandler(LoggingHandler):
        """OTLP attributes must be primitives: lift ``fields`` into flat record attributes."""

        def emit(self, record: logging.LogRecord) -> None:
            fields = getattr(record, "fields", None)
            if fields:
                record = logging.makeLogRecord(record.__dict__)
                del record.fields
                for key, value in fields.items():
                    setattr(record, key, value if isinstance(value, _PRIMITIVE) else str(value))
            super().emit(record)

    handler = FlatteningHandler(logger_provider=logger_provider)
    handler.addFilter(ToggleFilter(runtime, "logs_otlp"))
    return handler


def _file_handler(service: str) -> logging.Handler | None:
    s = settings_mod.settings
    for directory in (s.log_dir, os.path.join(os.getcwd(), "logs")):
        try:
            os.makedirs(directory, exist_ok=True)
            return RotatingFileHandler(os.path.join(directory, f"{service}.log"), maxBytes=25 * 1024 * 1024,
                                       backupCount=4, encoding="utf-8")
        except OSError:
            continue
    return None


def init_logging(service: str, runtime, telemetry) -> logging.Logger:
    s = settings_mod.settings
    service_name = telemetry.service_name
    logger = logging.getLogger(f"shopsim.{service}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    for f in list(logger.filters):
        logger.removeFilter(f)
    logger.addFilter(TraceContextFilter())
    logger.addFilter(MuteFilter(runtime))

    stdout = logging.StreamHandler()
    stdout.setFormatter(JsonFormatter(service_name))
    stdout.addFilter(ToggleFilter(runtime, "logs_stdout"))
    logger.addHandler(stdout)

    file_handler = _file_handler(service)
    if file_handler is not None:
        file_handler.setFormatter(JsonFormatter(service_name))
        file_handler.addFilter(ToggleFilter(runtime, "logs_file"))
        logger.addHandler(file_handler)

    syslog = SysLogHandler(address=(s.syslog_host, s.syslog_port), facility=SysLogHandler.LOG_LOCAL0,
                           socktype=socket.SOCK_DGRAM)
    syslog.append_nul = False
    syslog.setFormatter(SyslogFormatter(service_name))
    syslog.addFilter(ToggleFilter(runtime, "logs_syslog"))
    logger.addHandler(syslog)

    span_events = SpanEventHandler()
    span_events.addFilter(ToggleFilter(runtime, "log_span_events"))
    logger.addHandler(span_events)

    if telemetry.logger_provider is not None:
        logger.addHandler(_otlp_handler(telemetry.logger_provider, runtime))
    return logger
