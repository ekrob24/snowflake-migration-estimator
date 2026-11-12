"""Tests for the calibration fitting math (no Snowflake connection needed)."""

import math

import pytest

from sfestimate.calibrate import MIN_OVERHEAD_S, fit


def _runs(throughput, overhead, speedup=2 ** 0.8, compile_s=1.0):
    out = []
    for i, mb in enumerate([500, 1000, 2000, 4000]):
        xs = overhead + mb / throughput
        out.append({"name": f"q{i}", "size": "XSMALL", "mb": mb, "exec_s": xs,
                    "compile_s": compile_s, "cache_fraction": 0.5})
        out.append({"name": f"q{i}", "size": "SMALL", "mb": mb, "exec_s": xs / speedup,
                    "compile_s": compile_s, "cache_fraction": 0.5})
    return out


def test_recovers_known_constants():
    r = fit(_runs(throughput=800, overhead=0.5))
    assert r["throughput_mb_s_xs"] == pytest.approx(800, rel=0.01)
    assert r["overhead_s"] == pytest.approx(0.5, abs=0.01)
    assert r["scaling_efficiency"] == pytest.approx(0.8, abs=0.01)


def test_compile_time_reported_but_not_in_overhead():
    r = fit(_runs(throughput=800, overhead=0.5, compile_s=12.0))
    assert r["median_compile_s"] == pytest.approx(12.0)
    assert r["overhead_s"] < 1.0


def test_negative_intercept_falls_back_to_floor():
    runs = _runs(throughput=800, overhead=0.0)
    for r in runs:
        if r["size"] == "XSMALL":
            r["exec_s"] -= 0.3  # makes the fitted intercept negative
    r = fit(runs)
    assert r["overhead_s"] == MIN_OVERHEAD_S
    assert r["throughput_mb_s_xs"] > 0 and math.isfinite(r["throughput_mb_s_xs"])


def test_efficiency_clamped():
    assert fit(_runs(800, 0.5, speedup=1.0))["scaling_efficiency"] == 0.3
    assert fit(_runs(800, 0.5, speedup=3.0))["scaling_efficiency"] == 1.0


def test_too_few_queries_exits():
    with pytest.raises(SystemExit):
        fit([r for r in _runs(800, 0.5) if r["name"] == "q0"])
