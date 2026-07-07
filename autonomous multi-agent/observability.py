"""
observability.py — OpenTelemetry trace/span management for the Agentic Engineering Factory.

Every LangGraph node calls start_span() at entry and end_span() on exit.
In production, the exporter would ship to Jaeger/OTLP/LangSmith.
For the PoC, traces are printed to stdout in structured JSON.

Usage (in each node):
    from observability import tracer, start_span, end_span
    span_id = start_span(state["trace_id"], node_name="pm_agent")
    ...
    end_span(span_id, status="ok", details={...})
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

# ---------------------------------------------------------------------------
# Tracer bootstrap — call once at process start
# ---------------------------------------------------------------------------

_resource = Resource(attributes={"service.name": "agentic-engineering-factory"})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(_provider)

tracer = trace.get_tracer("agentic.onetrust")

# In-memory span registry so nodes can close their own spans
_active_spans: dict[str, Any] = {}


def init_trace() -> str:
    """Generate a new UUID4 trace ID for a full run."""
    return str(uuid.uuid4())


def start_span(trace_id: str, node_name: str) -> str:
    """
    Start a child span for a node.
    Returns a span_id string that must be passed to end_span().
    """
    span_id = str(uuid.uuid4())
    span = tracer.start_span(name=node_name, attributes={"trace_id": trace_id})
    _active_spans[span_id] = span

    _print_span_event(
        event="span_start",
        trace_id=trace_id,
        span_id=span_id,
        node=node_name,
        timestamp=_now(),
    )
    return span_id


def end_span(span_id: str, *, node_name: str, status: str = "ok", details: dict[str, Any] | None = None) -> None:
    """
    End a span and emit a structured log line.
    status: "ok" | "error" | "blocked"
    """
    span = _active_spans.pop(span_id, None)
    if span is not None:
        span.set_attribute("status", status)
        if details:
            span.set_attribute("details.json", json.dumps(details, default=str)[:4096])
        span.end()

    _print_span_event(
        event="span_end",
        span_id=span_id,
        node=node_name,
        status=status,
        timestamp=_now(),
        details=details or {},
    )


def build_audit_entry(
    *,
    node: str,
    trace_id: str,
    span_id: str,
    summary: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Return a structured AuditEntry dict to append to state['audit_trail']."""
    return {
        "node": node,
        "trace_id": trace_id,
        "span_id": span_id,
        "timestamp": _now(),
        "summary": summary,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _print_span_event(**kwargs: Any) -> None:
    print(f"[OTEL] {json.dumps(kwargs, default=str)}")
