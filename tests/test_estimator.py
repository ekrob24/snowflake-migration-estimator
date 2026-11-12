import math
from datetime import datetime, timedelta

import pytest

from sfestimate.estimator import SIZES, Assumptions, runtime_s, simulate
from sfestimate.models import QueryEvent, classify

T0 = datetime(2026, 1, 5, 10, 0, 0)


def ev(start=T0, duration=10.0, bytes_scanned=0, qtype="SELECT", cache=False, weight=1.0, qid="q"):
    return QueryEvent(query_id=qid, start=start, duration_s=duration, bytes_scanned=bytes_scanned,
                      query_type=qtype, cache_hit=cache, weight=weight)


def test_classify():
    assert classify(ev(qtype="SELECT")) == "interactive"
    for t in ("INSERT", "merge", "COPY", "CTAS", "CREATE_TABLE_AS_SELECT"):
        assert classify(ev(qtype=t)) == "batch"


# --- runtime_s ----------------------------------------------------------------

def test_runtime_bigger_warehouse_is_faster():
    a = Assumptions()
    e = ev(bytes_scanned=50_000_000_000)
    times = [runtime_s(e, n, a) for _, n in SIZES]
    assert all(t1 > t2 for t1, t2 in zip(times, times[1:]))
    assert all(t > a.overhead_s for t in times)


def test_runtime_from_bytes_matches_formula():
    a = Assumptions(source="bigquery")  # compression 4
    e = ev(bytes_scanned=6_000_000_000)
    expected_xs = a.overhead_s + 6_000 / 4 / a.throughput_mb_s_xs
    assert runtime_s(e, 1, a) == pytest.approx(expected_xs)
    expected_m = a.overhead_s + 6_000 / 4 / a.throughput_mb_s_xs / 4 ** a.scaling_efficiency
    assert runtime_s(e, 4, a) == pytest.approx(expected_m)


def test_runtime_zero_bytes_uses_source_duration():
    a = Assumptions(overhead_s=0.4)
    e = ev(duration=12.0, bytes_scanned=0)
    assert runtime_s(e, 1, a) == pytest.approx(12.4)
    assert runtime_s(e, 2, a) == pytest.approx(0.4 + 12.0 / 2 ** a.scaling_efficiency)


# --- simulate -----------------------------------------------------------------

def _sim(events, a, size="XS", nodes=1):
    return simulate(events, size, nodes, a, months_factor=1.0, target=10.0)


@pytest.mark.parametrize("auto_suspend", [60, 30, 300])
def test_single_query_billing(auto_suspend):
    a = Assumptions(overhead_s=0.0, auto_suspend_s=auto_suspend)
    r = _sim([ev(duration=10.0)], a)
    billed = max(10 + auto_suspend, 60)
    assert r.credits == pytest.approx(billed / 3600)
    assert r.running_hours == pytest.approx((10 + auto_suspend) / 3600)
    assert r.monthly_cost == pytest.approx(r.credits * a.price_per_credit)
    assert r.peak_clusters == 1


def test_billing_scales_with_nodes():
    a = Assumptions(overhead_s=0.0)
    e = ev(duration=10.0)
    xs = _sim([e], a)
    # On a Medium the zero-byte query runs faster, but credits/hour is 4x.
    m = _sim([e], a, "M", 4)
    runtime_m = 10.0 / 4 ** a.scaling_efficiency
    assert m.credits == pytest.approx(max(runtime_m + 60, 60) / 3600 * 4)
    assert m.credits > xs.credits


def test_queries_within_auto_suspend_merge():
    a = Assumptions(overhead_s=0.0, auto_suspend_s=60)
    q1 = ev(start=T0, duration=10.0, qid="1")
    q2 = ev(start=T0 + timedelta(seconds=40), duration=10.0, qid="2")  # 30 s idle gap
    r = _sim([q1, q2], a)
    # One period from 0 s to 50 s, plus 60 s auto-suspend.
    assert r.credits == pytest.approx((50 + 60) / 3600)
    assert r.running_hours == pytest.approx(110 / 3600)


def test_queries_beyond_auto_suspend_are_separate_periods():
    a = Assumptions(overhead_s=0.0, auto_suspend_s=60)
    q1 = ev(start=T0, duration=10.0, qid="1")
    q2 = ev(start=T0 + timedelta(seconds=200), duration=10.0, qid="2")
    r = _sim([q1, q2], a)
    assert r.credits == pytest.approx(2 * 70 / 3600)


def test_cache_hits_cost_nothing():
    a = Assumptions()
    r = _sim([ev(cache=True), ev(cache=True, start=T0 + timedelta(hours=1))], a)
    assert r.credits == 0
    assert r.monthly_cost == 0
    assert r.peak_clusters == 0
    # And they don't add to a real query's bill.
    real = ev(duration=10.0, qid="real")
    assert _sim([real, ev(cache=True)], a).credits == pytest.approx(_sim([real], a).credits)


def test_weighted_event_runs_back_to_back():
    a = Assumptions(overhead_s=0.0)
    r = _sim([ev(duration=10.0, weight=5.0)], a)
    assert r.credits == pytest.approx((50 + 60) / 3600)


def test_eight_concurrent_queries_need_one_cluster():
    a = Assumptions(overhead_s=0.0)
    r = _sim([ev(duration=30.0, qid=str(i)) for i in range(8)], a)
    assert r.peak_clusters == 1
    assert r.credits == pytest.approx(90 / 3600)


def test_more_than_eight_concurrent_queries_scale_out():
    a = Assumptions(overhead_s=0.0)
    r = _sim([ev(duration=30.0, qid=str(i)) for i in range(9)], a)
    assert r.peak_clusters == 2
    # The second cluster is billed only for the 30 s it is needed.
    assert r.credits == pytest.approx((90 + 30) / 3600)


def test_cluster_count_capped_by_max_clusters():
    a = Assumptions(overhead_s=0.0, max_clusters=2)
    r = _sim([ev(duration=30.0, qid=str(i)) for i in range(40)], a)
    assert r.peak_clusters == 2


def test_percentiles_and_target():
    a = Assumptions(overhead_s=0.0)
    events = [ev(duration=float(d), qid=str(d), start=T0 + timedelta(hours=d)) for d in range(1, 21)]
    r = simulate(events, "XS", 1, a, months_factor=1.0, target=10.0)
    assert r.p50_s == pytest.approx(10.5)
    assert r.p95_s == pytest.approx(19.05)
    assert not r.meets_target
    assert simulate(events, "XS", 1, a, 1.0, target=20.0).meets_target
    assert all(math.isfinite(x) for x in (r.credits, r.monthly_cost, r.running_hours))
