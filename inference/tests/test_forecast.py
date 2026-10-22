import numpy as np
import pandas as pd
import pytest
from conftest import blank_grid, set_w

from soilmon import clean, derive, events as ev, forecast as fc
from soilmon.simulator import simulate


def _prep(raw, cfg, until=None):
    if until is not None:
        raw = raw[raw["ts"].isna() | (raw["ts"] <= until)]
    ds = clean.normalize(raw, cfg, raw["ts"].max())
    derive.add_derived(ds, cfg)
    g = clean.to_grid(ds, cfg)
    return g, ev.detect_events(g, cfg)


def test_recovers_drying_coefficient_and_crossing_window(sim_clean, cfg0):
    raw, truth = sim_clean
    t_ev = truth["events"][1]
    now = t_ev + pd.Timedelta(hours=62)
    g, events = _prep(raw, cfg0, now)
    res = fc.dry_down(g, events, cfg0, g.index.max())
    assert res["status"] == "ok"
    assert res["fit"]["k"] == pytest.approx(truth["k"], rel=0.15)
    assert res["fit"]["k_lo"] <= truth["k"] <= res["fit"]["k_hi"] * 1.05
    w = truth["w"]
    cross = w[(w.index > now) & (w < cfg0.th.moisture.target_w)].index[0]
    true_h = (cross - g.index.max()).total_seconds() / 3600
    assert res["hours_lo"] - 8 <= true_h <= (res["hours_hi"] or 1e9) + 8
    assert res["hours_mid"] == pytest.approx(true_h, rel=0.3)
    assert res["hours_lo"] < res["hours_mid"] < res["hours_hi"]
    assert len(res["projection"]["t"]) == len(res["projection"]["mid"])
    assert all(a >= b >= c for a, b, c in zip(res["projection"]["hi"], res["projection"]["mid"], res["projection"]["lo"]))


def test_history_of_previous_dry_downs(sim_clean, cfg0):
    raw, truth = sim_clean
    g, events = _prep(raw, cfg0)
    res = fc.dry_down(g, events, cfg0, g.index.max())
    assert len(res["history"]) >= 2
    for h in res["history"]:
        assert h["k"] == pytest.approx(truth["k"], rel=0.2)


def test_insufficient_right_after_watering(sim_clean, cfg0):
    raw, truth = sim_clean
    now = truth["events"][1] + pd.Timedelta(hours=9)
    g, events = _prep(raw, cfg0, now)
    res = fc.dry_down(g, events, cfg0, g.index.max())
    assert res["status"] == "insufficient" and "need" in res["note"]


def test_flat_index_is_no_trend(cfg0):
    n = 5 * 96
    rng = np.random.default_rng(3)
    g = blank_grid(steps=n)
    g["vpd"] = 1.4
    set_w(g, 0.6 + rng.normal(0, 0.004, n))
    res = fc.dry_down(g, [], cfg0, g.index.max())
    assert res["status"] == "no_trend"


def test_already_below_target(cfg0):
    n = 4 * 96
    g = blank_grid(steps=n)
    g["vpd"] = 1.4
    set_w(g, np.linspace(0.7, 0.3, n))
    res = fc.dry_down(g, [], cfg0, g.index.max())
    assert res["status"] == "at_or_below" and res["w_now"] <= cfg0.th.moisture.target_w


def test_no_data(cfg0):
    assert fc.dry_down(blank_grid(steps=10), [], cfg0, pd.Timestamp("2026-10-01T03:00:00Z"))["status"] == "no_data"


def test_vpd_scaling_beats_plain_time_when_demand_varies(cfg0):
    """Same moisture loss per kPa-day, but VPD alternates between humid and dry days: the VPD-scaled fit sees the pattern."""
    n = 6 * 96
    v = np.where((np.arange(n) // 96) % 2 == 0, 0.5, 2.0)
    w = 0.95 - np.cumsum(0.1 * v * (15 / 1440.0))
    g = blank_grid(steps=n)
    g["vpd"] = v
    set_w(g, w)
    res = fc.dry_down(g, [], cfg0, g.index.max())
    assert res["fit"]["k"] == pytest.approx(0.1, rel=0.02)
    assert res["fit"]["resid_mad"] < 1e-3


def _decay(n, k, vpd=1.5, w0=0.95):
    return w0 - np.cumsum(np.full(n, k * vpd * 15 / 1440.0))


def test_drying_anomaly_detects_sustained_acceleration(cfg0):
    n = 6 * 96
    base = _decay(n, 0.05)
    accel = base.copy()
    m = 64                                                    # last 16 h dry out 3x faster
    accel[-m:] = base[-m] - np.cumsum(np.full(m, 3 * 0.05 * 1.5 * 15 / 1440.0))
    g = blank_grid(steps=n)
    g["vpd"] = 1.5
    set_w(g, accel)
    out = fc.drying_anomaly(g, [], cfg0)
    assert out["status"] == "anomaly" and out["k_now"] > out["threshold"]
    g2 = blank_grid(steps=n)
    g2["vpd"] = 1.5
    set_w(g2, base)
    assert fc.drying_anomaly(g2, [], cfg0)["status"] == "ok"


def test_drying_anomaly_needs_history(cfg0):
    g = blank_grid(steps=40)
    g["vpd"] = 1.5
    set_w(g, _decay(40, 0.05))
    assert fc.drying_anomaly(g, [], cfg0)["status"] == "insufficient"
