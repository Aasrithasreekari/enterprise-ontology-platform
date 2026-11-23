"""Structured logging, correlation IDs and Prometheus metrics."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Histogram

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid:
            payload["correlation_id"] = cid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)  # stderr keeps stdout clean for MCP stdio
    handler.setFormatter(JsonFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


REGISTRY = CollectorRegistry(auto_describe=True)

INGESTED_RECORDS = Counter(
    "op_ingested_records_total", "Source records processed", ["connector_type", "outcome"], registry=REGISTRY
)
GRAPH_WRITES = Counter("op_graph_writes_total", "Graph mutations", ["subject", "change"], registry=REGISTRY)
INGESTION_RUNS = Counter("op_ingestion_runs_total", "Ingestion runs", ["connector_type", "status"], registry=REGISTRY)
QUERY_LATENCY = Histogram("op_query_seconds", "Query latency", ["operation"], registry=REGISTRY)
TOOL_CALLS = Counter("op_mcp_tool_calls_total", "MCP tool calls", ["tool", "outcome"], registry=REGISTRY)
HTTP_REQUESTS = Counter("op_http_requests_total", "REST requests", ["method", "route", "status"], registry=REGISTRY)
