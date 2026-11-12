import math

import pytest

from sfestimate.estimator import Assumptions, estimate
from sfestimate.parsers import load
from sfestimate.report import html_report, text_summary
from sfestimate.samples import DAYS, PROFILES

SOURCES = ["redshift", "bigquery", "postgres"]


def run(sample_dir, profile, source, batch_target=None):
    path = sample_dir / f"{profile}_{source}.csv"
    kwargs = {"window_days": DAYS} if source == "postgres" else {}
    events = load(source, path, **kwargs)
    a = Assumptions(source=source)
    if batch_target is not None:
        a.target_p95_s = {**a.target_p95_s, "batch": batch_target}
    return estimate(events, a, window_days=DAYS)


def batch_of(est):
    return next(w for w in est.workloads if w.name == "batch")


def test_samples_generated(sample_dir):
    files = sorted(p.name for p in sample_dir.iterdir())
    assert len(files) == len(PROFILES) * 3


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("profile", list(PROFILES))
def test_estimate_end_to_end(sample_dir, profile, source):
    est = run(sample_dir, profile, source)
    assert {w.name for w in est.workloads} == {"interactive", "batch"}
    for w in est.workloads:
        rec = w.recommended
        assert w.queries > 0 and w.executions > 0
        for o in w.options:
            for x in (o.p50_s, o.p95_s, o.credits, o.monthly_credits, o.monthly_cost, o.running_hours):
                assert math.isfinite(x) and x > 0
        if any(o.meets_target for o in w.options):
            assert rec.meets_target
            assert w.note == ""
            # It is the cheapest option that meets the target.
            assert rec.monthly_cost == min(o.monthly_cost for o in w.options if o.meets_target)
        else:
            assert w.note
        assert w.cost_low <= rec.monthly_cost <= w.cost_high
        assert math.isfinite(w.cost_low) and w.cost_low > 0
    assert est.total_low <= est.total_monthly_cost <= est.total_high
    assert est.total_monthly_cost > 0
    d = est.to_dict()
    assert d["totals"]["total_monthly_cost"] == pytest.approx(est.total_monthly_cost, abs=0.01)


@pytest.mark.parametrize("source", SOURCES)
def test_batch_shop_batch_workload_is_heavier(sample_dir, source):
    # With the default 15-minute batch target, both profiles' batch jobs fit on an
    # X-Small (batch_shop p95 is ~4-7 min), and the estimator correctly picks the
    # cheapest size that meets the target. So compare load and cost here...
    shop, startup = batch_of(run(sample_dir, "batch_shop", source)), batch_of(run(sample_dir, "startup", source))
    assert shop.recommended.nodes >= startup.recommended.nodes
    assert shop.options[0].p95_s > 5 * startup.options[0].p95_s
    assert shop.recommended.monthly_cost > 3 * startup.recommended.monthly_cost


@pytest.mark.parametrize("source", SOURCES)
def test_batch_shop_needs_bigger_batch_warehouse_with_tight_target(sample_dir, source):
    # ...and size here, with a 2-minute batch target that startup meets on XS.
    shop = batch_of(run(sample_dir, "batch_shop", source, batch_target=120.0))
    startup = batch_of(run(sample_dir, "startup", source, batch_target=120.0))
    assert startup.recommended.size == "XS"
    assert shop.recommended.meets_target
    assert shop.recommended.nodes > startup.recommended.nodes


@pytest.mark.parametrize("source", SOURCES)
def test_reports_mention_recommended_size(sample_dir, source):
    est = run(sample_dir, "bi_team", source)
    text = text_summary(est)
    page = html_report(est)
    assert text.strip() and page.strip()
    assert page.lstrip().lower().startswith("<!doctype html>")
    for w in est.workloads:
        assert w.recommended.size in text
        assert f"{w.name} warehouse — {w.recommended.size}" in page
        assert f"{w.recommended.size} (recommended)" in page
