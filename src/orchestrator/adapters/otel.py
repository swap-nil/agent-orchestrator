"""OpenTelemetry setup (traces and metrics over OTLP/gRPC)."""

from __future__ import annotations

import os

from ..config import OrchestratorConfig


def configure_telemetry(config: OrchestratorConfig) -> None:
    if not config.telemetry.enabled:
        return
    # Pin the GenAI semantic-convention behaviour explicitly (conventions are still evolving).
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", config.telemetry.semconv_stability_opt_in)
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true" if config.telemetry.capture_content else "false"

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({
        "service.name": config.service.name,
        "deployment.environment": config.service.environment,
        "cloud.region": config.service.region,
        "orchestrator.cell": config.service.cell_id,
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=config.telemetry.otlp_endpoint)))
    trace.set_tracer_provider(provider)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=config.telemetry.otlp_endpoint))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
