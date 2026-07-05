from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Any


def elapsed_ms(started: float, ended: float | None = None) -> float:
    """Return a stable millisecond duration suitable for persisted metrics."""
    end = perf_counter() if ended is None else ended
    return round(max(0.0, end - started) * 1000, 3)


def datetime_delta_ms(started: datetime | None, ended: datetime | None) -> float | None:
    if started is None or ended is None:
        return None
    return round(max(0.0, (ended - started).total_seconds()) * 1000, 3)


def put_metric(metrics: dict[str, Any], name: str, value: Any) -> None:
    """Store JSON-safe, non-null metric values without replacing the metric map."""
    if value is not None:
        metrics[name] = value
