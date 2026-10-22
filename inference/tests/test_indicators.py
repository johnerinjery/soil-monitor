import numpy as np
import pandas as pd
import pytest
from conftest import blank_grid, set_w

from soilmon import clean, derive, indicators as ind
from soilmon.simulator import simulate

IST = "Asia/Kolkata"


def test_overwet_levels(cfg0):
    n = 6 * 96
    w = np.full(n, 0.5)
    for hours, level, dwell in ((30, "elevated", 24.0), (80, "high", 74.0), (5, "none", 0.0)):
        x = w.copy()
        x[-int(hours * 4):] = 0.97
        r = ind.overwet(set_w(blank_grid(steps=n), x), cfg0)
        assert r["level"] == level and r["dwell_h"] == pytest.approx(dwell) and r["run_h"] == pytest.approx(hours)


def test_overwet_ignores_trailing_gaps_and_dry_soil(cfg0):
    n = 400
    x = np.full(n, 0.5)
    x[-120:-8] = 0.97
    x[-8:] = np.nan
    r = ind.overwet(set_w(blank_grid(steps=n), x), cfg0)
    assert r["run_h"] == pytest.approx(28.0)
    assert ind.overwet(set_w(blank_grid(steps=n), np.full(n, 0.5)), cfg0)["level"] == "none"
    assert ind.overwet(blank_grid(steps=0), cfg0)["level"] == "none"


def test_humid_air_integrator_exact_numbers(cfg0):
    g = blank_grid(steps=72)                                  # 18 h
    g["air_t_clean"] = 22.0
    g["rh_clean"] = np.r_[np.full(40, 90.0), np.full(32, 50.0)]
    r = ind.humid_air(g, cfg0)
    assert r["hours"] == pytest.approx(10 - 0.5 * 8)           # +10 h, then 8 h of decay at 0.5
    assert r["series"].iloc[39] == pytest.approx(10.0) and r["level"] == "none"


def test_humid_air_floor_levels_and_temperature_window(cfg0):
    g = blank_grid(steps=4 * 96)
    g["air_t_clean"] = 22.0
    g["rh_clean"] = 90.0
    assert ind.humid_air(g, cfg0)["level"] == "high"           # 96 h
    g["air_t_clean"] = 3.0                                     # too cold for fungal growth: no accumulation
    assert ind.humid_air(g, cfg0)["hours"] == 0.0
    g["air_t_clean"] = 22.0
    g["rh_clean"] = 40.0
    assert ind.humid_air(g, cfg0)["hours"] == 0.0              # never negative


def test_humid_air_unknown_data_freezes_the_integrator(cfg0):
    g = blank_grid(steps=40)
    g["air_t_clean"] = 22.0
    g["rh_clean"] = 90.0
    g.loc[g.index[20:30], "rh_clean"] = np.nan
    assert ind.humid_air(g, cfg0)["hours"] == pytest.approx(30 / 4)


@pytest.mark.parametrize("v,band", [(0.2, "low"), (0.8, "ok"), (1.4, "warn"), (2.0, "high")])
def test_vpd_bands(cfg0, v, band):
    g = blank_grid(steps=20)
    g["vpd"] = v
    g["vpd_sigma"] = 0.1
    assert ind.vpd_state(g, cfg0)["band"] == band


def _light_grid(days=3, on=(8, 18), ppfd=100.0, missing=0.0, seed=0):
    start = pd.Timestamp("2026-10-01 00:00", tz=IST).tz_convert("UTC")
    g = blank_grid(start=start, steps=days * 96)
    hl = g.index.tz_convert(IST).hour + g.index.tz_convert(IST).minute / 60
    p = np.where((hl >= on[0]) & (hl < on[1]), ppfd, 0.0)
    g["ppfd"] = p
    g["lux_clean"] = p / 0.0165
    if missing:
        rng = np.random.default_rng(seed)
        g.loc[rng.random(len(g)) < missing, ["ppfd", "lux_clean"]] = np.nan
    return g


def test_dli_exact_and_complete(cfg0):
    daily, heat = ind.daily_summary(_light_grid(), [], cfg0)
    d0 = daily.iloc[0]
    assert d0["dli"] == pytest.approx(3.6, rel=1e-6) and d0["complete"] and d0["coverage"] == 1.0
    assert d0["photoperiod_h"] == pytest.approx(10.0)
    assert d0["dli_unc"] == pytest.approx(3.6 * np.hypot(0.20, 0.25), rel=0.01)
    assert len(heat["dates"]) == 3 and len(heat["z"]) == 24
    assert heat["z"][12][0] == pytest.approx(100.0) and heat["z"][3][0] == 0.0


def test_dli_with_missing_samples(cfg0):
    daily, _ = ind.daily_summary(_light_grid(missing=0.15, seed=1), [], cfg0)
    d = daily.iloc[0]
    assert d["complete"] and d["dli"] == pytest.approx(3.6, rel=0.12)       # gap-filled by coverage
    daily, _ = ind.daily_summary(_light_grid(missing=0.5, seed=1), [], cfg0)
    d = daily.iloc[0]
    assert not d["complete"] and d["dli"] is None and 0 < d["dli_so_far"] < 3.0


def test_current_partial_day_is_never_complete(cfg0):
    g = _light_grid(days=2)
    g = g.iloc[: 96 + 50]                                      # second day only half observed
    daily, _ = ind.daily_summary(g, [], cfg0)
    assert not daily.iloc[-1]["complete"]
    assert daily.iloc[-1]["coverage"] == pytest.approx(1.0)


def test_saturation_is_counted(cfg0):
    g = _light_grid()
    g.loc[g.index[40:44], "lux_sat"] = 1.0
    daily, _ = ind.daily_summary(g, [], cfg0)
    assert daily["sat_bins"].sum() == 4


def test_light_state_classification(cfg0):
    for ppfd, expect in ((40.0, "low"), (100.0, "low"), (150.0, "uncertain"), (180.0, "ok"), (400.0, "uncertain"), (500.0, "high")):
        daily, _ = ind.daily_summary(_light_grid(days=3, ppfd=ppfd), [], cfg0)
        st = ind.light_state(daily, None, cfg0)
        assert st["status"] == expect, (ppfd, st)
    daily, _ = ind.daily_summary(_light_grid(days=1).iloc[:60], [], cfg0)
    assert ind.light_state(daily, None, cfg0)["status"] == "no_complete_day"


@pytest.mark.parametrize("tau", [1.5, 4.0, 10.0])
def test_thermal_lag_recovers_time_constant(cfg0, tau):
    raw, truth = simulate(cfg0, days=8, seed=5, glitches=False, tau_true_h=tau)
    ds = clean.normalize(raw, cfg0, raw["ts"].max())
    derive.add_derived(ds, cfg0)
    r = ind.thermal_lag(clean.to_grid(ds, cfg0), cfg0)
    assert r["status"] == "ok" and r["tau_h"] == pytest.approx(tau, rel=0.15)
    assert 0 < r["amplitude_ratio_24h"] < 1 and r["r2"] > 0.8
    assert r["observed_range_ratio"] == pytest.approx(r["amplitude_ratio_24h"], abs=0.15)


def test_thermal_lag_needs_data_and_coupling(cfg0):
    assert ind.thermal_lag(blank_grid(steps=96), cfg0)["status"] == "insufficient"
    g = blank_grid(steps=4 * 96)
    rng = np.random.default_rng(0)
    g["air_t_clean"] = 24 + 3 * np.sin(np.arange(len(g)) / 15.0)
    g["soil_t_clean"] = 20 + rng.normal(0, 0.05, len(g))        # soil ignores the air entirely
    r = ind.thermal_lag(g, cfg0)
    assert r["status"] in ("no_coupling", "ok")
    if r["status"] == "ok":
        assert r["tau_h"] > 48


def test_condensation(cfg0):
    g = blank_grid(steps=40)
    for margin, level in ((5.0, "ok"), (1.5, "warn"), (0.2, "crit")):
        g["margin"] = margin
        assert ind.condensation(g, cfg0)["level"] == level
    assert ind.condensation(blank_grid(steps=10), cfg0)["level"] == "unknown"


def test_comfort(cfg0):
    g = blank_grid(steps=96)
    g["air_t_clean"] = 22.0
    g["rh_clean"] = np.r_[np.full(48, 50.0), np.full(48, 85.0)]
    r = ind.comfort(g, cfg0)
    assert r["frac_in_range"] == pytest.approx(0.5, abs=0.02) and r["cold_h"] == 0 and r["hot_h"] == 0


def test_probe_spread_flag_and_outlier(cfg0):
    class DS:
        report = {}
    n = 96
    g = set_w(blank_grid(steps=n), m1=np.full(n, 0.5), m2=np.full(n, 0.52), m3=np.r_[np.full(n - 16, 0.5), np.full(16, 0.95)])
    q = ind.quality(DS, g, [], cfg0)
    assert q["spread"]["flag"] and q["spread"]["outlier"] == "m3" and q["spread"]["run_h"] == pytest.approx(4.0)
    g2 = set_w(blank_grid(steps=n), m1=np.full(n, 0.5), m2=np.full(n, 0.6), m3=np.full(n, 0.55))
    assert not ind.quality(DS, g2, [], cfg0)["spread"]["flag"]


def test_plateau_drift(cfg0):
    class DS:
        report = {}
    evs = [{"probe_peaks": {"m1": v, "m2": 0.95, "m3": 0.95}} for v in (0.95, 0.94, 0.85, 0.72)]
    q = ind.quality(DS, blank_grid(steps=10), evs, cfg0)
    assert q["plateau_drift"]["m1"]["flag"] and not q["plateau_drift"]["m2"]["flag"]
