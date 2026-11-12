"""Turn source query history into Snowflake warehouse sizes and monthly cost.

Model, in four steps:

1. Runtime. Each query's Snowflake runtime on an X-Small warehouse is a fixed
   overhead plus (bytes scanned / scan throughput). Bytes are first converted from
   the source's accounting to Snowflake's compressed columnar bytes. On a warehouse
   with N nodes the scan part shrinks by N ** scaling_efficiency (perfect scaling
   would be 1.0). If the source didn't record bytes, the source runtime is used.
2. Billing. Snowflake bills a running warehouse per second, with a 60-second
   minimum each time it resumes, and keeps it running until it has been idle for
   AUTO_SUSPEND seconds. We replay the query timeline to find those running periods.
3. Concurrency. A warehouse cluster runs about MAX_CONCURRENCY_LEVEL (default 8)
   queries at once. When more overlap, a multi-cluster warehouse adds clusters; we
   bill the extra clusters only for the seconds they're needed.
4. Sizing. For each workload we pick the cheapest size whose 95th-percentile query
   time meets that workload's target.

Every number here is an assumption you can override, and `sfestimate calibrate`
measures the runtime constants on your own Snowflake account.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .models import QueryEvent, classify

# Warehouse size -> credits per hour (= relative node count). Standard warehouses.
SIZES: list[tuple[str, int]] = [
    ("XS", 1), ("S", 2), ("M", 4), ("L", 8),
    ("XL", 16), ("2XL", 32), ("3XL", 64), ("4XL", 128),
]

# How many source bytes equal one Snowflake (compressed, columnar) byte scanned.
# BigQuery reports logical uncompressed bytes; Postgres reads uncompressed row pages;
# Redshift reports compressed columnar blocks, like Snowflake.
DEFAULT_COMPRESSION = {"redshift": 1.0, "bigquery": 4.0, "postgres": 3.0}


@dataclass
class Assumptions:
    source: str = "redshift"
    compression_ratio: float | None = None   # None -> DEFAULT_COMPRESSION[source]
    throughput_mb_s_xs: float = 150.0         # Snowflake MB scanned per second on XS
    overhead_s: float = 0.4                   # compile + scheduling per query
    scaling_efficiency: float = 0.85          # 1.0 = doubling size halves scan time
    auto_suspend_s: int = 60
    min_billing_s: int = 60
    max_concurrency: int = 8
    max_clusters: int = 10
    price_per_credit: float = 3.00            # USD; Enterprise edition list price, AWS US
    target_p95_s: dict = field(default_factory=lambda: {"interactive": 10.0, "batch": 900.0})
    storage_tb: float = 0.0
    storage_price_tb_month: float = 23.0      # USD; capacity pricing, AWS US
    calibrated_from: str = ""

    @property
    def compression(self) -> float:
        if self.compression_ratio is not None:
            return self.compression_ratio
        return DEFAULT_COMPRESSION.get(self.source, 1.0)

    def apply_calibration(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text())
        for key in ("throughput_mb_s_xs", "overhead_s", "scaling_efficiency"):
            if key in data:
                setattr(self, key, float(data[key]))
        self.calibrated_from = str(data.get("measured_on", path))


def runtime_s(event: QueryEvent, nodes: int, a: Assumptions) -> float:
    """Estimated Snowflake execution time for one query on a warehouse with `nodes` nodes."""
    if event.bytes_scanned > 0:
        sf_mb = event.bytes_scanned / a.compression / 1e6
        work_xs = sf_mb / a.throughput_mb_s_xs
    else:
        work_xs = event.duration_s
    return a.overhead_s + work_xs / (nodes ** a.scaling_efficiency)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass
class SizeResult:
    size: str
    nodes: int
    p50_s: float
    p95_s: float
    credits: float            # over the observed window
    monthly_credits: float
    monthly_cost: float
    running_hours: float      # over the observed window
    peak_clusters: int
    meets_target: bool


def simulate(events: list[QueryEvent], size: str, nodes: int, a: Assumptions,
             months_factor: float, target: float) -> SizeResult:
    """Replay the timeline on one warehouse size and bill it like Snowflake would."""
    runs = [e for e in events if not e.cache_hit]
    latencies = [runtime_s(e, nodes, a) for e in runs]

    # Busy intervals. A weighted event stands for `weight` executions, run back to back.
    intervals = sorted(
        (e.start, e.start + timedelta(seconds=t * e.weight)) for e, t in zip(runs, latencies)
    )

    # Running periods: merge intervals separated by less than the auto-suspend delay.
    billed_s = 0.0
    running_s = 0.0
    if intervals:
        gap = timedelta(seconds=a.auto_suspend_s)
        cur_s, cur_e = intervals[0]
        periods = []
        for s, e in intervals[1:]:
            if s <= cur_e + gap:
                cur_e = max(cur_e, e)
            else:
                periods.append((cur_s, cur_e))
                cur_s, cur_e = s, e
        periods.append((cur_s, cur_e))
        for s, e in periods:
            length = (e - s).total_seconds() + a.auto_suspend_s
            running_s += length
            billed_s += max(length, a.min_billing_s)

    # Extra clusters: sweep concurrency; bill extra clusters only while needed.
    points: list[tuple[datetime, int]] = []
    for s, e in intervals:
        points.append((s, 1))
        points.append((e, -1))
    points.sort(key=lambda p: (p[0], p[1]))
    extra_cluster_s = 0.0
    conc = 0
    peak_clusters = 1 if intervals else 0
    prev_t = None
    for t, delta in points:
        if prev_t is not None and conc > a.max_concurrency:
            clusters = min(math.ceil(conc / a.max_concurrency), a.max_clusters)
            extra_cluster_s += (t - prev_t).total_seconds() * (clusters - 1)
            peak_clusters = max(peak_clusters, clusters)
        conc += delta
        prev_t = t

    credits = (billed_s + extra_cluster_s) / 3600 * nodes
    p95 = _percentile(latencies, 0.95)
    return SizeResult(
        size=size,
        nodes=nodes,
        p50_s=_percentile(latencies, 0.50),
        p95_s=p95,
        credits=credits,
        monthly_credits=credits * months_factor,
        monthly_cost=credits * months_factor * a.price_per_credit,
        running_hours=running_s / 3600,
        peak_clusters=peak_clusters,
        meets_target=p95 <= target,
    )


@dataclass
class WorkloadResult:
    name: str
    queries: int
    executions: float
    cache_hits: int
    target_p95_s: float
    recommended: SizeResult
    options: list[SizeResult]
    cost_low: float       # monthly cost if Snowflake scans 2x faster than assumed
    cost_high: float      # monthly cost if Snowflake scans 2x slower than assumed
    queries_by_hour: list[float]
    note: str = ""


@dataclass
class Estimate:
    source: str
    window_start: datetime
    window_end: datetime
    window_days: float
    workloads: list[WorkloadResult]
    assumptions: Assumptions

    @property
    def compute_monthly_cost(self) -> float:
        return sum(w.recommended.monthly_cost for w in self.workloads)

    @property
    def compute_monthly_credits(self) -> float:
        return sum(w.recommended.monthly_credits for w in self.workloads)

    @property
    def storage_monthly_cost(self) -> float:
        return self.assumptions.storage_tb * self.assumptions.storage_price_tb_month

    @property
    def total_monthly_cost(self) -> float:
        return self.compute_monthly_cost + self.storage_monthly_cost

    @property
    def total_low(self) -> float:
        return sum(w.cost_low for w in self.workloads) + self.storage_monthly_cost

    @property
    def total_high(self) -> float:
        return sum(w.cost_high for w in self.workloads) + self.storage_monthly_cost

    def to_dict(self) -> dict:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat()
        d["window_end"] = self.window_end.isoformat()
        d["totals"] = {
            "compute_monthly_credits": round(self.compute_monthly_credits, 2),
            "compute_monthly_cost": round(self.compute_monthly_cost, 2),
            "storage_monthly_cost": round(self.storage_monthly_cost, 2),
            "total_monthly_cost": round(self.total_monthly_cost, 2),
            "total_monthly_cost_range": [round(self.total_low, 2), round(self.total_high, 2)],
        }
        return d


def _choose(options: list[SizeResult]) -> tuple[SizeResult, str]:
    ok = [o for o in options if o.meets_target]
    if ok:
        return min(ok, key=lambda o: (o.monthly_cost, o.nodes)), ""
    best = min(options, key=lambda o: o.p95_s)
    return best, ("No size meets the p95 target. The largest queries are probably "
                  "better served by splitting them out or by clustering/search optimization.")


def estimate(events: list[QueryEvent], a: Assumptions, window_days: float | None = None) -> Estimate:
    if not events:
        raise ValueError("No queries to estimate.")
    for e in events:
        if not e.workload:
            e.workload = classify(e)

    w_start = min(e.start for e in events)
    w_end = max(e.start + timedelta(seconds=e.duration_s) for e in events)
    if window_days is None:
        window_days = max((w_end - w_start).total_seconds() / 86400, 1 / 24)
    months_factor = 30.0 / window_days

    results: list[WorkloadResult] = []
    for name in sorted({e.workload for e in events}):
        group = [e for e in events if e.workload == name]
        target = a.target_p95_s.get(name, a.target_p95_s.get("interactive", 10.0))
        options = [simulate(group, s, n, a, months_factor, target) for s, n in SIZES]
        rec, note = _choose(options)

        # Sensitivity: the scan-throughput assumption dominates; bracket it at 2x either way.
        def at_throughput(mult: float) -> float:
            b = Assumptions(**{**asdict(a), "throughput_mb_s_xs": a.throughput_mb_s_xs * mult})
            alt = [simulate(group, s, n, b, months_factor, target) for s, n in SIZES]
            return _choose(alt)[0].monthly_cost

        fast, slow = at_throughput(2.0), at_throughput(0.5)
        by_hour = [0.0] * 24
        for e in group:
            by_hour[e.start.hour] += e.weight
        days = max(window_days, 1.0)
        results.append(WorkloadResult(
            name=name,
            queries=len(group),
            executions=sum(e.weight for e in group),
            cache_hits=sum(1 for e in group if e.cache_hit),
            target_p95_s=target,
            recommended=rec,
            options=options,
            cost_low=min(fast, slow, rec.monthly_cost),
            cost_high=max(fast, slow, rec.monthly_cost),
            queries_by_hour=[round(v / days, 1) for v in by_hour],
            note=note,
        ))

    return Estimate(
        source=a.source,
        window_start=w_start,
        window_end=w_end,
        window_days=window_days,
        workloads=results,
        assumptions=a,
    )
