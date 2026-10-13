"""OpenTelemetry wiring for one service: traces + metrics (+ optional logs) over OTLP/HTTP.

Each service gets its own providers (not the global ones) so tests can run all three services in
one process with the right ``service.name`` on each. Runtime switches (traces on/off, sample
ratio, metrics on/off, simulated outage) are honoured by a sampler and an exporter wrapper that
read the live config, so nothing needs a restart.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult

from shopsim import settings as settings_mod
from shopsim.settings import VERSION

_MASK64 = (1 << 64) - 1


class _RuntimeRatio(Sampler):
    """TraceIdRatioBased, but the ratio is read from the live config on every root span."""

    def __init__(self, runtime):
        self.runtime = runtime

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None,
                      trace_state=None) -> SamplingResult:
        ratio = self.runtime.eff.telemetry.sample_ratio
        if ratio >= 1 or (trace_id & _MASK64) < int(ratio * (1 << 64)):
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)
        return SamplingResult(Decision.DROP)

    def get_description(self) -> str:
        return "ShopSimRuntimeRatio"


class RuntimeSampler(Sampler):
    def __init__(self, runtime):
        self.runtime = runtime
        self._inner = ParentBased(root=_RuntimeRatio(runtime))

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None,
                      trace_state=None) -> SamplingResult:
        if not self.runtime.eff.telemetry.traces or self.runtime.muted:
            return SamplingResult(Decision.DROP)
        return self._inner.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return "ShopSimRuntimeSampler"


class ToggleMetricExporter(MetricExporter):
    """Drops exports while metrics are switched off (or the service is 'down')."""

    def __init__(self, inner: MetricExporter, runtime):
        super().__init__(preferred_temporality=inner._preferred_temporality,
                         preferred_aggregation=inner._preferred_aggregation)
        self.inner = inner
        self.runtime = runtime

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        if not self.runtime.eff.telemetry.metrics or self.runtime.muted:
            return MetricExportResult.SUCCESS
        return self.inner.export(metrics_data, timeout_millis=timeout_millis, **kwargs)

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self.inner.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        self.inner.shutdown(timeout_millis=timeout_millis, **kwargs)


@dataclass
class Telemetry:
    service_name: str
    resource: Resource
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider | None
    span_exporter: InMemorySpanExporter | None = None
    metric_reader: InMemoryMetricReader | None = None
    tracer: trace.Tracer = field(init=False)

    def __post_init__(self):
        self.tracer = self.tracer_provider.get_tracer("shopsim", VERSION)
        self.meter = self.meter_provider.get_meter("shopsim", VERSION)

    def shutdown(self, deadline_s: float = 5.0) -> None:
        """Flush and stop, but never hang the process if the collector is gone."""
        def run():
            for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
                if provider is not None:
                    try:
                        provider.shutdown()
                    except Exception:  # noqa: BLE001 - best effort on the way out
                        pass

        worker = threading.Thread(target=run, name="otel-shutdown", daemon=True)
        worker.start()
        worker.join(deadline_s)


def init_telemetry(service: str, runtime) -> Telemetry:
    s = settings_mod.settings
    service_name = f"shopsim-{service}"
    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": "shopsim",
        "service.version": VERSION,
        "service.instance.id": f"{s.hostname}-{service}-{os.getpid()}",
        "deployment.environment": s.environment,
        "deployment.environment.name": s.environment,
        "host.name": s.hostname,
    })
    tracer_provider = TracerProvider(resource=resource, sampler=RuntimeSampler(runtime), shutdown_on_exit=False)
    span_exporter = metric_reader = logger_provider = None
    readers = []

    if s.exporters == "otlp":
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        base = s.otlp_endpoint.rstrip("/")
        tracer_provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{base}/v1/traces", timeout=5),
            max_queue_size=20_000, max_export_batch_size=512, schedule_delay_millis=2000))
        readers.append(PeriodicExportingMetricReader(
            ToggleMetricExporter(OTLPMetricExporter(endpoint=f"{base}/v1/metrics", timeout=5), runtime),
            export_interval_millis=int(s.metric_interval_s * 1000)))
        logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs", timeout=5)))
    elif s.exporters == "memory":
        span_exporter = InMemorySpanExporter()
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        metric_reader = InMemoryMetricReader()
        readers.append(metric_reader)

    meter_provider = MeterProvider(metric_readers=readers, resource=resource, shutdown_on_exit=False)
    return Telemetry(service_name, resource, tracer_provider, meter_provider, logger_provider,
                     span_exporter=span_exporter, metric_reader=metric_reader)
