"""The normalized query record every source parser produces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Statement types that write data. Everything else is treated as a read.
WRITE_TYPES = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "UNLOAD", "CTAS",
    "CREATE_TABLE_AS_SELECT", "CREATE TABLE AS", "TRUNCATE_TABLE", "VACUUM",
}


@dataclass
class QueryEvent:
    """One query execution, normalized across source systems."""

    query_id: str
    start: datetime          # naive UTC
    duration_s: float        # wall-clock time on the source system
    bytes_scanned: int       # bytes read on the source system; 0 if unknown
    query_type: str          # upper-case statement type, e.g. SELECT, INSERT
    user: str = ""
    cache_hit: bool = False  # served from the source's result cache
    weight: float = 1.0      # >1 when one row stands in for several executions
    workload: str = ""       # filled in by classify()


def classify(event: QueryEvent) -> str:
    """Route a query to a workload. Each workload gets its own Snowflake warehouse.

    Splitting reads from writes is the standard Snowflake pattern: dashboards stay
    fast while batch loads run on a separately sized warehouse.
    """
    return "batch" if event.query_type.upper() in WRITE_TYPES else "interactive"
