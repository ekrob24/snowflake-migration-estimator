"""Generate realistic sample query-history exports for demos and tests.

Each profile describes a kind of company. We simulate two weeks of its queries once,
then write that same workload in each source system's export format, so the three
sample files describe the same business.
"""

from __future__ import annotations

import csv
import math
import random
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

START = datetime(2026, 1, 5)  # a Monday
DAYS = 14


@dataclass
class Profile:
    name: str
    description: str
    reads_per_weekday: int
    read_gb_median: float      # logical (uncompressed) GB per dashboard/ad-hoc query
    read_gb_sigma: float
    batch_jobs_per_day: int
    batch_gb_median: float
    batch_gb_sigma: float
    batch_hours: tuple[int, int]
    cache_rate: float          # share of reads answered from the result cache


PROFILES = {
    "startup": Profile(
        "startup", "Small analytics team: a few hundred queries a day, light nightly ETL.",
        reads_per_weekday=300, read_gb_median=0.3, read_gb_sigma=1.2,
        batch_jobs_per_day=20, batch_gb_median=2.0, batch_gb_sigma=0.8,
        batch_hours=(1, 3), cache_rate=0.15),
    "bi_team": Profile(
        "bi_team", "Busy BI org: thousands of dashboard queries in business hours, hourly loads.",
        reads_per_weekday=3000, read_gb_median=1.5, read_gb_sigma=1.4,
        batch_jobs_per_day=48, batch_gb_median=8.0, batch_gb_sigma=0.9,
        batch_hours=(0, 24), cache_rate=0.30),
    "batch_shop": Profile(
        "batch_shop", "Heavy nightly batch: large transformations, few interactive users.",
        reads_per_weekday=120, read_gb_median=2.0, read_gb_sigma=1.0,
        batch_jobs_per_day=60, batch_gb_median=60.0, batch_gb_sigma=0.9,
        batch_hours=(0, 6), cache_rate=0.05),
}

# Source engines' effective throughput on logical GB, used to fake source runtimes.
SOURCE_GB_PER_S = {"redshift": 0.8, "bigquery": 2.5, "postgres": 0.15}


@dataclass
class _Q:
    i: int
    start: datetime
    logical_bytes: int
    is_write: bool
    user: str
    cache_hit: bool


def _business_time(rng: random.Random, day: datetime) -> datetime:
    # Peaks mid-morning and mid-afternoon.
    hour = rng.choice([9, 10, 10, 11, 11, 12, 13, 14, 14, 15, 15, 16, 17])
    return day + timedelta(hours=hour, seconds=rng.uniform(0, 3600))


def simulate_workload(profile: Profile, seed: int = 42) -> list[_Q]:
    rng = random.Random(seed)
    qs: list[_Q] = []
    users = [f"analyst_{k}" for k in range(1, 13)] + ["dashboard_svc"] * 6
    for d in range(DAYS):
        day = START + timedelta(days=d)
        weekday = day.weekday() < 5
        n_reads = profile.reads_per_weekday if weekday else profile.reads_per_weekday // 10
        for _ in range(n_reads):
            gb = rng.lognormvariate(math.log(profile.read_gb_median), profile.read_gb_sigma)
            qs.append(_Q(len(qs), _business_time(rng, day), int(gb * 1e9), False,
                         rng.choice(users), rng.random() < profile.cache_rate))
        lo, hi = profile.batch_hours
        for _ in range(profile.batch_jobs_per_day):
            gb = rng.lognormvariate(math.log(profile.batch_gb_median), profile.batch_gb_sigma)
            ts = day + timedelta(seconds=rng.uniform(lo * 3600, hi * 3600))
            qs.append(_Q(len(qs), ts, int(gb * 1e9), True, "etl_svc", False))
    qs.sort(key=lambda q: q.start)
    return qs


def _src_duration(rng: random.Random, q: _Q, source: str) -> float:
    if q.cache_hit:
        return rng.uniform(0.05, 0.3)
    base = 0.5 + q.logical_bytes / 1e9 / SOURCE_GB_PER_S[source]
    return base * rng.uniform(0.7, 1.4)


def write_redshift(qs: list[_Q], path: Path, seed: int = 1) -> None:
    rng = random.Random(seed)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query_id", "user_id", "query_type", "status", "start_time", "end_time",
                    "elapsed_time", "result_cache_hit", "scanned_bytes"])
        for q in qs:
            dur = _src_duration(rng, q, "redshift")
            end = q.start + timedelta(seconds=dur)
            qtype = rng.choice(["INSERT", "INSERT", "CTAS", "UPDATE", "COPY"]) if q.is_write else "SELECT"
            scanned = 0 if q.cache_hit else int(q.logical_bytes / 4)  # compressed columnar
            w.writerow([100000 + q.i, zlib.crc32(q.user.encode()) % 500, qtype, "success",
                        q.start.isoformat(sep=" "), end.isoformat(sep=" "),
                        int(dur * 1_000_000), "t" if q.cache_hit else "f", scanned])


def write_bigquery(qs: list[_Q], path: Path, seed: int = 2) -> None:
    rng = random.Random(seed)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["job_id", "creation_time", "start_time", "end_time", "user_email", "job_type",
                    "statement_type", "total_bytes_processed", "total_slot_ms", "cache_hit",
                    "state", "error_result"])
        for q in qs:
            dur = _src_duration(rng, q, "bigquery")
            end = q.start + timedelta(seconds=dur)
            stype = rng.choice(["INSERT", "MERGE", "CREATE_TABLE_AS_SELECT"]) if q.is_write else "SELECT"
            processed = 0 if q.cache_hit else q.logical_bytes
            slot_ms = int(dur * 1000 * rng.uniform(20, 200))
            ts = lambda t: t.strftime("%Y-%m-%d %H:%M:%S.%f UTC")
            w.writerow([f"bquxjob_{q.i:08x}", ts(q.start), ts(q.start), ts(end),
                        f"{q.user}@example.com", "QUERY", stype, processed, slot_ms,
                        "true" if q.cache_hit else "false", "DONE", ""])


def write_postgres(qs: list[_Q], path: Path, seed: int = 3) -> None:
    """Aggregate into ~100 normalized statements, as pg_stat_statements does."""
    rng = random.Random(seed)
    templates: dict[int, dict] = {}
    read_tmpl = [f"SELECT region, sum(amount) FROM sales_{k} WHERE day >= $1 GROUP BY region"
                 for k in range(80)]
    write_tmpl = [f"INSERT INTO mart_{k} SELECT * FROM staging_{k} WHERE loaded_at > $1"
                  for k in range(20)]
    for q in qs:
        if q.cache_hit:
            continue  # Postgres has no result cache; those reads would just run
        key = (80 + q.i % 20) if q.is_write else (q.i % 80)
        t = templates.setdefault(key, {"calls": 0, "ms": 0.0, "blks": 0, "rows": 0})
        t["calls"] += 1
        t["ms"] += _src_duration(rng, q, "postgres") * 1000
        t["blks"] += q.logical_bytes // 8192
        t["rows"] += rng.randint(1, 500)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["userid", "queryid", "query", "calls", "total_exec_time", "mean_exec_time",
                    "rows", "shared_blks_hit", "shared_blks_read"])
        for key, t in sorted(templates.items()):
            sql = write_tmpl[key - 80] if key >= 80 else read_tmpl[key]
            hit = int(t["blks"] * 0.6)
            w.writerow([16384, 9_000_000_000 + key, sql, t["calls"], round(t["ms"], 3),
                        round(t["ms"] / t["calls"], 3), t["rows"], hit, t["blks"] - hit])


def generate(out_dir: str | Path = "samples") -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, profile in PROFILES.items():
        qs = simulate_workload(profile)
        for source, writer in (("redshift", write_redshift), ("bigquery", write_bigquery),
                               ("postgres", write_postgres)):
            p = out / f"{name}_{source}.csv"
            writer(qs, p)
            written.append(p)
    return written
