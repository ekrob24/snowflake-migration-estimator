"""Render an Estimate as a terminal summary and a self-contained HTML report."""

from __future__ import annotations

import html
from datetime import datetime

from .estimator import Estimate

SOURCE_NAMES = {"redshift": "Amazon Redshift", "bigquery": "Google BigQuery", "postgres": "PostgreSQL"}


def _money(x: float) -> str:
    return f"${x:,.0f}"


def _secs(x: float) -> str:
    if x < 1:
        return f"{x * 1000:.0f} ms"
    if x < 120:
        return f"{x:.1f} s"
    return f"{x / 60:.1f} min"


def text_summary(est: Estimate) -> str:
    a = est.assumptions
    lines = [
        f"Source: {SOURCE_NAMES.get(est.source, est.source)}  |  "
        f"window: {est.window_days:.1f} days  |  price: ${a.price_per_credit:.2f}/credit",
        "",
        f"{'Workload':<12} {'Size':<5} {'p95':>9} {'Target':>9} {'Credits/mo':>11} {'Cost/mo':>10}",
    ]
    for w in est.workloads:
        r = w.recommended
        lines.append(
            f"{w.name:<12} {r.size:<5} {_secs(r.p95_s):>9} {_secs(w.target_p95_s):>9} "
            f"{r.monthly_credits:>11,.0f} {_money(r.monthly_cost):>10}"
        )
        if w.note:
            lines.append(f"  ! {w.note}")
    lines += [
        "",
        f"Compute: {_money(est.compute_monthly_cost)}/month   Storage: {_money(est.storage_monthly_cost)}/month",
        f"Total:   {_money(est.total_monthly_cost)}/month  "
        f"(range {_money(est.total_low)} – {_money(est.total_high)})",
    ]
    if not a.calibrated_from:
        lines.append("Note: using default runtime constants. Run `sfestimate calibrate` to measure them.")
    return "\n".join(lines)


def _hour_chart(values: list[float], label: str) -> str:
    w, h, pad_l, pad_b, pad_t = 600, 160, 44, 22, 10
    top = max(values) or 1.0
    bw = (w - pad_l) / 24
    bars = []
    for i, v in enumerate(values):
        bh = (h - pad_b - pad_t) * v / top
        x = pad_l + i * bw + 1
        y = h - pad_b - bh
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 2:.1f}" height="{bh:.1f}" rx="2" class="bar">'
            f"<title>{i:02d}:00 UTC — {v:,.0f} queries/day</title></rect>"
        )
    ticks = "".join(
        f'<text x="{pad_l + i * bw + bw / 2:.1f}" y="{h - 6}" class="tick" text-anchor="middle">{i:02d}</text>'
        for i in range(0, 24, 3)
    )
    ylab = (f'<text x="{pad_l - 6}" y="{pad_t + 8}" class="tick" text-anchor="end">{top:,.0f}</text>'
            f'<text x="{pad_l - 6}" y="{h - pad_b}" class="tick" text-anchor="end">0</text>')
    axis = f'<line x1="{pad_l}" y1="{h - pad_b}" x2="{w}" y2="{h - pad_b}" class="axis"/>'
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(label)}">'
            f'{axis}{"".join(bars)}{ticks}{ylab}</svg>')


CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1b2430;--muted:#5d6b7a;--line:#e3e7ec;--accent:#1f7ae0;--ok:#1a7f4b;--warn:#b25e00;--rec:#e8f2fd}
@media (prefers-color-scheme:dark){:root{--bg:#0f1419;--card:#171d24;--ink:#e6edf3;--muted:#93a1b0;--line:#2a333d;--accent:#5aa6f5;--ok:#4cc38a;--warn:#f0a050;--rec:#13263b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 12px}
.sub{color:var(--muted);margin:0 0 24px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}.card .k{color:var(--muted);font-size:13px}
.card .v{font-size:26px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}.card .s{color:var(--muted);font-size:13px}
.wl{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 16px 8px;margin-bottom:16px}
.wl h3{margin:0 0 4px;font-size:16px;text-transform:capitalize}.wl p{margin:0 0 12px;color:var(--muted);font-size:14px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{color:var(--muted);font-weight:600}
th:first-child,td:first-child{text-align:left}tr.rec td{background:var(--rec);font-weight:600}
.ok{color:var(--ok)}.no{color:var(--warn)}.note{color:var(--warn);font-size:14px}
svg{width:100%;height:auto;display:block;margin:4px 0 12px}.bar{fill:var(--accent)}.tick{fill:var(--muted);font-size:10px}.axis{stroke:var(--line)}
ul{padding-left:20px}li{margin:4px 0}code{font-size:13px}footer{color:var(--muted);font-size:13px;margin-top:40px}
"""


def html_report(est: Estimate, title: str = "Snowflake Migration Estimate") -> str:
    a = est.assumptions
    src = SOURCE_NAMES.get(est.source, est.source)
    e = html.escape

    cards = f"""
<div class="cards">
  <div class="card"><div class="k">Estimated monthly cost</div><div class="v">{_money(est.total_monthly_cost)}</div>
    <div class="s">range {_money(est.total_low)} – {_money(est.total_high)}</div></div>
  <div class="card"><div class="k">Compute credits / month</div><div class="v">{est.compute_monthly_credits:,.0f}</div>
    <div class="s">at ${a.price_per_credit:.2f} per credit</div></div>
  <div class="card"><div class="k">Warehouses</div><div class="v">{len(est.workloads)}</div>
    <div class="s">{", ".join(f"{w.name}: {w.recommended.size}" for w in est.workloads)}</div></div>
  <div class="card"><div class="k">Queries analyzed</div><div class="v">{sum(w.executions for w in est.workloads):,.0f}</div>
    <div class="s">over {est.window_days:.1f} days of {e(src)} history</div></div>
</div>"""

    blocks = []
    for w in est.workloads:
        rows = []
        for o in w.options:
            cls = ' class="rec"' if o is w.recommended else ""
            ok = '<span class="ok">✓</span>' if o.meets_target else '<span class="no">✗</span>'
            rows.append(
                f"<tr{cls}><td>{o.size}{' (recommended)' if o is w.recommended else ''}</td>"
                f"<td>{_secs(o.p50_s)}</td><td>{_secs(o.p95_s)} {ok}</td>"
                f"<td>{o.running_hours / est.window_days:.1f} h</td><td>{o.peak_clusters}</td>"
                f"<td>{o.monthly_credits:,.0f}</td><td>{_money(o.monthly_cost)}</td></tr>"
            )
        purpose = ("Dashboards and ad-hoc reads" if w.name == "interactive"
                   else "Loads and transformations (INSERT, MERGE, COPY, CTAS…)")
        note = f'<p class="note">{e(w.note)}</p>' if w.note else ""
        blocks.append(f"""
<div class="wl">
  <h3>{e(w.name)} warehouse — {w.recommended.size}</h3>
  <p>{purpose}. {w.queries:,} queries ({w.cache_hits:,} result-cache hits, billed at zero).
     Target: 95% of queries finish within {_secs(w.target_p95_s)}.</p>{note}
  {_hour_chart(w.queries_by_hour, f"{w.name} queries per day by hour")}
  <div class="tw"><table>
    <thead><tr><th>Size</th><th>Median</th><th>p95</th><th>Running / day</th><th>Peak clusters</th>
      <th>Credits / mo</th><th>Cost / mo</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</div>""")

    calib = (f"Runtime constants measured on Snowflake ({e(a.calibrated_from)})." if a.calibrated_from
             else "Runtime constants are defaults, not yet measured on Snowflake. Run "
                  "<code>sfestimate calibrate</code> to measure them on your account.")
    assumptions = f"""
<ul>
  <li>{calib}</li>
  <li>Scan speed on an X-Small warehouse: <b>{a.throughput_mb_s_xs:,.0f} MB/s</b> of compressed data,
      plus <b>{a.overhead_s:.2f} s</b> fixed overhead per query.</li>
  <li>Each size doubling makes the scan part {2 ** a.scaling_efficiency:.2f}× faster (efficiency {a.scaling_efficiency:.2f}).</li>
  <li>{e(src)} bytes ÷ <b>{a.compression:.1f}</b> = Snowflake bytes scanned (compression / format difference).</li>
  <li>Auto-suspend after {a.auto_suspend_s} s idle; {a.min_billing_s} s minimum per resume;
      up to {a.max_concurrency} concurrent queries per cluster, then multi-cluster scale-out.</li>
  <li>${a.price_per_credit:.2f} per credit; storage {a.storage_tb:g} TB × ${a.storage_price_tb_month:.0f}/TB-month.
      Check current pricing for your edition, cloud, and region.</li>
  <li>The cost range re-runs the estimate with scan speed halved and doubled.</li>
</ul>"""

    caveats = """
<ul>
  <li>Not included: serverless features (Snowpipe, auto-clustering, search optimization, tasks),
      cloud-services credits above the 10% daily allowance, data transfer, and migration effort.</li>
  <li>Queries are replayed at their original start times; Snowflake's own result cache and
      warehouse cache would usually lower cost further.</li>
  <li>This is an estimate for planning a proof of concept, not a quote.</li>
</ul>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head>
<body><main>
<h1>{e(title)}</h1>
<p class="sub">From {e(src)} query history, {est.window_start:%b %d, %Y} – {est.window_end:%b %d, %Y}
 · generated {datetime.now():%b %d, %Y}</p>
{cards}
<h2>Recommended warehouses</h2>
{"".join(blocks)}
<h2>Assumptions</h2>{assumptions}
<h2>What this does not cover</h2>{caveats}
<footer>Generated by snowflake-migration-estimator. Community project; not affiliated with Snowflake Inc.</footer>
</main></body></html>"""
