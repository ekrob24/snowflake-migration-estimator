"""Command-line interface: `sfestimate <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .estimator import Assumptions, estimate
from .parsers import SOURCES, load
from .report import html_report, text_summary


def cmd_samples(args) -> None:
    from .samples import generate
    for p in generate(args.out):
        print(f"wrote {p}")


def cmd_estimate(args) -> None:
    kwargs = {}
    if args.source == "postgres":
        if not args.window_days:
            sys.exit("--window-days is required for postgres (time since pg_stat_statements was reset).")
        kwargs["window_days"] = args.window_days
    events = load(args.source, args.file, **kwargs)

    a = Assumptions(
        source=args.source,
        compression_ratio=args.compression,
        price_per_credit=args.price_per_credit,
        storage_tb=args.storage_tb,
        storage_price_tb_month=args.storage_price,
        auto_suspend_s=args.auto_suspend,
        target_p95_s={"interactive": args.interactive_p95, "batch": args.batch_p95},
    )
    if args.calibration:
        a.apply_calibration(args.calibration)
    elif Path("calibration.json").exists():
        a.apply_calibration("calibration.json")
        print("Using calibration.json")

    est = estimate(events, a, window_days=args.window_days)
    print(text_summary(est))
    if args.html:
        Path(args.html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.html).write_text(html_report(est), encoding="utf-8")
        print(f"\nHTML report: {args.html}")
    if args.json:
        Path(args.json).write_text(json.dumps(est.to_dict(), indent=2, default=str))
        print(f"JSON: {args.json}")


def cmd_keygen(args) -> None:
    from .snowflake_conn import PRIVATE_KEY, PUBLIC_KEY, keygen
    sql = keygen(force=args.force)
    print(f"Wrote {PRIVATE_KEY} (private, never commit) and {PUBLIC_KEY}.\n")
    print("Run this once in a Snowsight worksheet to register the key:\n")
    print(sql)


def cmd_check(args) -> None:
    from .snowflake_conn import check
    print(check())


def cmd_calibrate(args) -> None:
    from .calibrate import calibrate
    print(f"Running TPC-H SF{args.scale} benchmark on X-Small and Small warehouses "
          "(well under 1 credit)...")
    r = calibrate(warehouse=args.warehouse, scale=args.scale, out=args.out)
    print(f"\nthroughput_mb_s_xs = {r['throughput_mb_s_xs']}\n"
          f"overhead_s         = {r['overhead_s']}\n"
          f"scaling_efficiency = {r['scaling_efficiency']}\n"
          f"Saved to {args.out}. `sfestimate estimate` picks it up automatically.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sfestimate",
        description="Estimate Snowflake warehouse sizing and cost from another warehouse's query history.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("samples", help="write sample query-history CSVs")
    s.add_argument("--out", default="samples")
    s.set_defaults(func=cmd_samples)

    e = sub.add_parser("estimate", help="estimate Snowflake cost from a query-history CSV")
    e.add_argument("file")
    e.add_argument("--source", required=True, choices=SOURCES)
    e.add_argument("--html", help="write an HTML report to this path")
    e.add_argument("--json", help="write the full result as JSON")
    e.add_argument("--price-per-credit", type=float, default=3.0)
    e.add_argument("--storage-tb", type=float, default=0.0)
    e.add_argument("--storage-price", type=float, default=23.0, help="USD per TB-month")
    e.add_argument("--auto-suspend",type=int, default=60, help="seconds")
    e.add_argument("--interactive-p95", type=float, default=10.0, help="target seconds")
    e.add_argument("--batch-p95", type=float, default=900.0, help="target seconds")
    e.add_argument("--compression", type=float, help="source bytes per Snowflake byte")
    e.add_argument("--calibration", help="calibration.json from `sfestimate calibrate`")
    e.add_argument("--window-days", type=float, help="history length in days (required for postgres)")
    e.set_defaults(func=cmd_estimate)

    k = sub.add_parser("keygen", help="create a key pair for Snowflake key-pair sign-in")
    k.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_keygen)

    c = sub.add_parser("check", help="test the Snowflake connection from .env")
    c.set_defaults(func=cmd_check)

    cal = sub.add_parser("calibrate", help="measure runtime constants on your Snowflake account")
    cal.add_argument("--warehouse", default="SFESTIMATE_CALIBRATION_WH")
    cal.add_argument("--scale", type=int, default=100, choices=[1, 10, 100])
    cal.add_argument("--out", default="calibration.json")
    cal.set_defaults(func=cmd_calibrate)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ValueError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
