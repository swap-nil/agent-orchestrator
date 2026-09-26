"""Trace context handling.

The orchestrator continues the W3C trace started by the token service and
carried by the master agent. When OpenTelemetry is installed and configured
(see ``adapters/otel.py``) spans are exported; otherwise a no-op tracer is used
so the core never depends on telemetry being available.

Span names follow the OpenTelemetry GenAI conventions where they apply:
``invoke_agent <agent>`` for A2A calls and ``execute_tool <tool>`` for tools.
"""

from __future__ import annotations

import contextlib
import re
import secrets
from dataclasses import dataclass
from typing import Any, Iterator

_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    flags: str = "01"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"

    def child(self) -> "TraceContext":
        return TraceContext(self.trace_id, secrets.token_hex(8), self.flags)


def new_trace() -> TraceContext:
    return TraceContext(secrets.token_hex(16), secrets.token_hex(8))


def parse_traceparent(value: str | None) -> TraceContext | None:
    """Parse a W3C traceparent header. Returns None if absent or invalid."""
    if not value:
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if not match:
        return None
    version, trace_id, span_id, flags = match.groups()
    if version == "ff" or trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return TraceContext(trace_id, span_id, flags)


def continue_or_start(traceparent: str | None) -> TraceContext:
    parsed = parse_traceparent(traceparent)
    return parsed.child() if parsed else new_trace()


# --------------------------------------------------------------------------- span shim

try:  # pragma: no cover - exercised only when OpenTelemetry is installed
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator as _Propagator

    _HAS_OTEL = True
except ImportError:  # pragma: no cover
    _HAS_OTEL = False


@contextlib.contextmanager
def span(name: str, ctx: TraceContext | None = None, **attributes: Any) -> Iterator[None]:
    """Open a span when OpenTelemetry is available, otherwise do nothing."""
    if not _HAS_OTEL:
        yield
        return
    tracer = _otel_trace.get_tracer("orchestrator")  # pragma: no cover
    parent = None  # pragma: no cover
    if ctx is not None:  # pragma: no cover
        parent = _Propagator().extract({"traceparent": ctx.traceparent})
    clean = {k: v for k, v in attributes.items() if v is not None}  # pragma: no cover
    with tracer.start_as_current_span(name, context=parent, attributes=clean):  # pragma: no cover
        yield
