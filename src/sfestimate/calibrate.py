"""Measure the estimator's runtime constants on a real Snowflake account.

Runs a few TPC-H queries from SNOWFLAKE_SAMPLE_DATA on fresh X-Small and Small
warehouses with the result cache off, reads back bytes scanned and execution time,
and fits:
  overhead_s + MB / throughput_mb_s_xs  ~= runtime on X-Small
  scaling_efficiency = log2(XS scan time / S scan time)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .snowflake_conn import connect

TPCH = {
    "q1_pricing_summary": """
        SELECT l_returnflag, l_linestatus, SUM(l_quantity), SUM(l_extendedprice),
               SUM(l_extendedprice * (1 - l_discount)), AVG(l_discount), COUNT(*)
        FROM {db}.lineitem WHERE l_shipdate <= DATEADD(day, -90, '1998-12-01')
        GROUP BY 1, 2 ORDER BY 1, 2""",
    "q3_shipping_priority": """
        SELECT l_orderkey, SUM(l_extendedprice * (1 - l_discount)) AS revenue, o_orderdate
        FROM {db}.customer, {db}.orders, {db}.lineitem
        WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey
          AND o_orderdate < '1995-03-15' AND l_shipdate > '1995-03-15'
        GROUP BY l_orderkey, o_orderdate ORDER BY revenue DESC LIMIT 10""",
    "q5_local_supplier": """
        SELECT n_name, SUM(l_extendedprice * (1 - l_discount)) AS revenue
        FROM {db}.customer, {db}.orders, {db}.lineitem, {db}.supplier, {db}.nation, {db}.region
        WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey AND l_suppkey = s_suppkey
          AND c_nationkey = s_nationkey AND s_nationkey = n_nationkey AND n_regionkey = r_regionkey
          AND r_name = 'ASIA' AND o_orderdate >= '1994-01-01' AND o_orderdate < '1995-01-01'
        GROUP BY n_name ORDER BY revenue DESC""",
    "q6_forecast_revenue": """
        SELECT SUM(l_extendedprice * l_discount) FROM {db}.lineitem
        WHERE l_shipdate >= '1994-01-01' AND l_shipdate < '1995-01-01'
          AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24""",
    "q10_returned_items": """
        SELECT c_custkey, c_name, SUM(l_extendedprice * (1 - l_discount)) AS revenue
        FROM {db}.customer, {db}.orders, {db}.lineitem, {db}.nation
        WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey AND o_orderdate >= '1993-10-01'
          AND o_orderdate < '1994-01-01' AND l_returnflag = 'R' AND c_nationkey = n_nationkey
        GROUP BY c_custkey, c_name ORDER BY revenue DESC LIMIT 20""",
    "q12_shipping_modes": """
        SELECT l_shipmode, COUNT(*) FROM {db}.orders, {db}.lineitem
        WHERE o_orderkey = l_orderkey AND l_shipmode IN ('MAIL', 'SHIP')
          AND l_receiptdate >= '1994-01-01' AND l_receiptdate < '1995-01-01'
        GROUP BY l_shipmode ORDER BY l_shipmode""",
    "full_scan_lineitem": "SELECT COUNT(DISTINCT l_partkey), SUM(l_extendedprice) FROM {db}.lineitem",
}


def _fit_line(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least squares y = a + b x. Returns (a, b)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def _run_on(cur, warehouse: str, size: str, db: str) -> list[dict]:
    cur.execute(f"CREATE WAREHOUSE IF NOT EXISTS {warehouse} WAREHOUSE_SIZE = '{size}' "
                "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE")
    cur.execute(f"ALTER WAREHOUSE {warehouse} SET WAREHOUSE_SIZE = '{size}'")
    cur.execute(f"USE WAREHOUSE {warehouse}")
    ids = {}
    for name, sql in TPCH.items():
        print(f"  {size:>2}  {name} ...", flush=True)
        cur.execute(sql.format(db=db))
        cur.fetchall()
        ids[cur.sfqid] = name
    runs = []
    for qid, name in ids.items():
        # Per-operator stats are the reliable source for bytes scanned; the query-history
        # summary can report 0 for multi-table queries.
        cur.execute(f"""
            SELECT SUM(operator_statistics:io:bytes_scanned::NUMBER),
                   SUM(operator_statistics:io:bytes_scanned::NUMBER
                       * operator_statistics:io:percentage_scanned_from_cache::FLOAT)
            FROM TABLE(GET_QUERY_OPERATOR_STATS('{qid}'))
            WHERE operator_type = 'TableScan'""")
        scanned, cached = cur.fetchone()
        runs.append({"query_id": qid, "name": name, "size": size,
                     "bytes": int(scanned or 0), "cache_fraction": (cached or 0) / max(scanned or 0, 1)})
    cur.execute(f"ALTER WAREHOUSE {warehouse} SUSPEND")
    return runs


def calibrate(warehouse: str = "SFESTIMATE_CALIBRATION_WH", scale: int = 100,
              out: str | Path = "calibration.json") -> dict:
    db = f"SNOWFLAKE_SAMPLE_DATA.TPCH_SF{scale}"
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
        try:
            cur.execute(f"SELECT 1 FROM {db}.region LIMIT 1")
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"Can't read {db}: {exc}\nIf the sample database is missing, run this in Snowsight:\n"
                "  CREATE DATABASE SNOWFLAKE_SAMPLE_DATA FROM SHARE SFC_SAMPLES.SAMPLE_DATA;"
            )
        # Separate warehouses so the Small run doesn't benefit from the X-Small's local cache.
        runs = _run_on(cur, f"{warehouse}_XS", "XSMALL", db)
        runs += _run_on(cur, f"{warehouse}_S", "SMALL", db)

        # INFORMATION_SCHEMA table functions need a current database; use a small one of our own.
        cur.execute("CREATE DATABASE IF NOT EXISTS SFESTIMATE COMMENT = 'snowflake-migration-estimator'")
        cur.execute("USE DATABASE SFESTIMATE")
        id_list = ",".join(f"'{r['query_id']}'" for r in runs)
        cur.execute(f"""
            SELECT query_id, execution_time, compilation_time
            FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT => 1000))
            WHERE query_id IN ({id_list})""")
        stats = {r[0]: r[1:] for r in cur.fetchall()}

    for r in runs:
        ex, comp = stats.get(r["query_id"], (0, 0))
        r.update(mb=r["bytes"] / 1e6, exec_s=(ex or 0) / 1000, compile_s=(comp or 0) / 1000)

    result = fit(runs)
    result["measured_on"] = f"TPC-H SF{scale}, {datetime.now(timezone.utc):%Y-%m-%d}"
    result["queries"] = runs
    Path(out).write_text(json.dumps(result, indent=2))
    return result


MIN_OVERHEAD_S = 0.2


def fit(runs: list[dict]) -> dict:
    """Fit the runtime constants from measured runs.

    Only warehouse execution time is fitted. Compilation happens in Snowflake's cloud
    services layer, and on a one-off benchmark against shared sample data it is far
    slower than for a production query that runs repeatedly, so it is reported
    (median_compile_s) but not folded into the per-query overhead.
    """
    xs = [r for r in runs if r["size"] == "XSMALL" and r["mb"] > 0 and r["exec_s"] > 0]
    sm = {r["name"]: r for r in runs if r["size"] == "SMALL" and r["mb"] > 0}
    if len(xs) < 3:
        raise SystemExit("Not enough query statistics came back to calibrate. Try again in a minute.")

    overhead, slope = _fit_line([r["mb"] for r in xs], [r["exec_s"] for r in xs])
    if slope <= 0 or overhead < 0:
        # Too few points for a clean line: use the aggregate rate and the floor overhead.
        overhead = MIN_OVERHEAD_S
        slope = sum(r["exec_s"] - overhead for r in xs) / sum(r["mb"] for r in xs)
    throughput = 1 / slope
    overhead = max(overhead, MIN_OVERHEAD_S)

    pairs = [(x["exec_s"], sm[x["name"]]["exec_s"]) for x in xs if x["name"] in sm]
    ratio = sum(p[0] for p in pairs) / max(sum(p[1] for p in pairs), 1e-9) if pairs else 2 ** 0.85
    efficiency = min(max(math.log2(ratio), 0.3), 1.0)

    compiles = sorted(r["compile_s"] for r in runs)
    return {
        "throughput_mb_s_xs": round(throughput, 1),
        "overhead_s": round(overhead, 3),
        "scaling_efficiency": round(efficiency, 3),
        "median_compile_s": round(compiles[len(compiles) // 2], 3) if compiles else None,
        "avg_cache_fraction": round(sum(r.get("cache_fraction", 0) for r in xs) / len(xs), 3),
    }
