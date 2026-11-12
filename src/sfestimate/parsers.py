"""Read query-history exports from each source system into QueryEvents.

Each parser expects the CSV produced by the matching file in export_queries/.
Column names are matched case-insensitively.
"""

from __future__ import annotations

import csv
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .models import QueryEvent

SOURCES = ("redshift", "bigquery", "postgres")


def parse_ts(value: str) -> datetime:
    """Parse the timestamp formats the three exports produce into naive UTC."""
    v = re.sub(r"(\d)T(\d)", r"\1 \2", value.strip())
    if v.endswith(" UTC"):
        v = v[:-4]
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # Trim fractional seconds beyond microseconds, which fromisoformat rejects.
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"t", "true", "1", "yes", "y"}


def _num(value: str, default: float = 0.0) -> float:
    try:
        return float(value) if str(value).strip() != "" else default
    except ValueError:
        return default


def _rows(path: Path) -> Iterable[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            yield {k.strip().lower(): (v or "") for k, v in row.items() if k}


def _require(row: dict, cols: list[str], source: str) -> None:
    missing = [c for c in cols if c not in row]
    if missing:
        raise ValueError(
            f"{source} export is missing column(s) {missing}. "
            f"Use the query in export_queries/{source}.sql to produce the CSV."
        )


def parse_redshift(path: Path) -> list[QueryEvent]:
    """SYS_QUERY_HISTORY joined to SYS_QUERY_DETAIL (see export_queries/redshift.sql)."""
    out: list[QueryEvent] = []
    for i, r in enumerate(_rows(path)):
        if i == 0:
            _require(r, ["query_id", "start_time", "elapsed_time", "query_type"], "redshift")
        if r.get("status", "success").strip().lower() not in {"success", ""}:
            continue
        qtype = r["query_type"].strip().upper()
        if qtype in {"UTILITY", "DDL", "OTHER"}:
            continue
        out.append(QueryEvent(
            query_id=r["query_id"],
            start=parse_ts(r["start_time"]),
            duration_s=_num(r["elapsed_time"]) / 1_000_000,  # microseconds
            bytes_scanned=int(_num(r.get("scanned_bytes", "0"))),
            query_type=qtype,
            user=r.get("user_id", ""),
            cache_hit=_truthy(r.get("result_cache_hit", "")),
        ))
    return out


def parse_bigquery(path: Path) -> list[QueryEvent]:
    """INFORMATION_SCHEMA.JOBS (see export_queries/bigquery.sql)."""
    out: list[QueryEvent] = []
    for i, r in enumerate(_rows(path)):
        if i == 0:
            _require(r, ["job_id", "start_time", "end_time", "total_bytes_processed"], "bigquery")
        if r.get("job_type", "QUERY").upper() != "QUERY":
            continue
        if r.get("state", "DONE").upper() != "DONE" or r.get("error_result", "").strip():
            continue
        stype = r.get("statement_type", "SELECT").strip().upper() or "SELECT"
        if stype == "SCRIPT":  # parent of child jobs that are listed separately
            continue
        start, end = parse_ts(r["start_time"]), parse_ts(r["end_time"])
        out.append(QueryEvent(
            query_id=r["job_id"],
            start=start,
            duration_s=max((end - start).total_seconds(), 0.0),
            bytes_scanned=int(_num(r["total_bytes_processed"])),
            query_type=stype,
            user=r.get("user_email", ""),
            cache_hit=_truthy(r.get("cache_hit", "")),
        ))
    return out


_PG_SKIP = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|SET|SHOW|RESET|DISCARD|DEALLOCATE|SAVEPOINT|RELEASE)\b", re.I)
_PG_TYPE = re.compile(r"^\s*(?:WITH\b.*?\)\s*)?(SELECT|INSERT|UPDATE|DELETE|MERGE|COPY|CREATE\s+TABLE\s+\w+\s+AS)", re.I | re.S)


def _pg_type(sql: str) -> str:
    m = _PG_TYPE.match(sql)
    if not m:
        return "OTHER"
    t = m.group(1).upper()
    return "CTAS" if t.startswith("CREATE") else t


def parse_postgres(
    path: Path,
    window_days: float = 7.0,
    end: datetime | None = None,
    max_events_per_statement: int = 200,
    seed: int = 7,
) -> list[QueryEvent]:
    """pg_stat_statements (see export_queries/postgres.sql).

    pg_stat_statements only keeps totals, not individual timestamps, so we spread each
    statement's calls over the observation window: reads during weekday business
    hours, writes overnight. Statements with many calls are sampled, and each sampled
    event carries a weight so totals stay correct.
    """
    rng = random.Random(seed)
    # pg_stat_statements covers the period up to "now" (when it was exported).
    end = end or datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=window_days)
    weekdays = [start + timedelta(days=d) for d in range(int(window_days))
                if (start + timedelta(days=d)).weekday() < 5] or [start]
    all_days = [start + timedelta(days=d) for d in range(max(int(window_days), 1))]

    out: list[QueryEvent] = []
    for i, r in enumerate(_rows(path)):
        if i == 0:
            _require(r, ["queryid", "query", "calls", "total_exec_time"], "postgres")
        sql = r["query"]
        if _PG_SKIP.match(sql):
            continue
        qtype = _pg_type(sql)
        if qtype == "OTHER":
            continue
        calls = int(_num(r["calls"]))
        if calls <= 0:
            continue
        mean_s = _num(r["total_exec_time"]) / calls / 1000  # ms total -> s per call
        blocks = _num(r.get("shared_blks_hit", "0")) + _num(r.get("shared_blks_read", "0"))
        bytes_per_call = int(blocks * 8192 / calls)
        n = min(calls, max_events_per_statement)
        weight = calls / n
        is_write = qtype != "SELECT"
        for k in range(n):
            if is_write:
                day = rng.choice(all_days)
                ts = day.replace(hour=0) + timedelta(seconds=rng.uniform(1 * 3600, 5 * 3600))
            else:
                day = rng.choice(weekdays)
                ts = day.replace(hour=0) + timedelta(seconds=rng.uniform(8 * 3600, 18 * 3600))
            out.append(QueryEvent(
                query_id=f"{r['queryid']}-{k}",
                start=ts,
                duration_s=mean_s,
                bytes_scanned=bytes_per_call,
                query_type=qtype,
                user=r.get("userid", ""),
                weight=weight,
            ))
    return out


PARSERS: dict[str, Callable[..., list[QueryEvent]]] = {
    "redshift": parse_redshift,
    "bigquery": parse_bigquery,
    "postgres": parse_postgres,
}


def load(source: str, path: str | Path, **kwargs) -> list[QueryEvent]:
    if source not in PARSERS:
        raise ValueError(f"Unknown source {source!r}. Choose one of: {', '.join(SOURCES)}")
    events = PARSERS[source](Path(path), **kwargs)
    if not events:
        raise ValueError(f"No usable queries found in {path}.")
    return sorted(events, key=lambda e: e.start)
