import numpy as np
import pandas as pd
import pytest

from soilmon import clean


def _row(ts, **kw):
    base = dict(device_id="node-01", boot_id=1, seq=0, ts=ts, m1_mv=1800, m2_mv=1800, m3_mv=1800, m1_mad=3, m2_mad=3, m3_mad=3,
                soil_t_c=22.0, air_t_c=24.0, rh_pct=55.0, lux=500.0, vbat_mv=None, rssi=-60, flags=0, fw="1.0.1")
    base.update(kw)
    return base


def _df(rows):
    out = pd.DataFrame(rows)
    out["seq"] = range(len(out))
    return out


NOW = pd.Timestamp("2026-10-20T12:00:00Z")


def test_probe_states(cfg):
    ts = pd.date_range("2026-10-20T00:00:00Z", periods=6, freq="15min")
    rows = [
        _row(ts[0]),
        _row(ts[1], m1_mv=142, m1_mad=0),                      # floating pin
        _row(ts[2], m1_mv=3295),                               # unplugged probe
        _row(ts[3], m1_mad=40),                                # loose wire
        _row(ts[4], flags=1),                                  # firmware said suspicious
        _row(ts[5], m1_mv=None),
    ]
    ds = clean.normalize(_df(rows), cfg, NOW)
    assert list(ds.samples["m1_state"]) == ["ok", "floating", "open", "noisy", "flagged", "null"]
    assert list(ds.samples["m1_ok"]) == [True, False, False, False, False, False]
    assert ds.samples["m1_mv_clean"].notna().sum() == 1
    assert ds.samples["m2_ok"].all()                           # other probes unaffected


def test_timestamps_duplicates_and_device(cfg):
    ts = pd.date_range("2026-10-20T00:00:00Z", periods=4, freq="15min")
    rows = [_row(ts[0]), _row(None), _row(pd.Timestamp("2020-01-01T00:00:00Z")), _row(pd.Timestamp("2027-06-01T00:00:00Z")),
            _row(ts[1], device_id="other-node"), _row(ts[2]), _row(ts[3])]
    df = _df(rows)
    df.loc[5, "seq"] = 0                                       # same (device, boot, seq) as row 0 -> duplicate
    ds = clean.normalize(df, cfg, NOW)
    r = ds.report
    assert r["rows_no_ts"] == 1 and r["rows_bad_ts"] == 2 and r["rows_other_device"] == 1 and r["rows_duplicate"] == 1
    assert len(ds.samples) == 2
    assert ds.samples.index.is_monotonic_increasing


def test_invalid_environment_values(cfg):
    ts = pd.date_range("2026-10-20T00:00:00Z", periods=3, freq="15min")
    rows = [_row(ts[0], rh_pct=105.0), _row(ts[1], flags=16), _row(ts[2], soil_t_c=85.0, lux=-5.0)]
    s = clean.normalize(_df(rows), cfg, NOW).samples
    assert np.isnan(s["rh_clean"].iloc[0]) and np.isnan(s["air_t_clean"].iloc[1]) and np.isnan(s["rh_clean"].iloc[1])
    assert np.isnan(s["soil_t_clean"].iloc[2]) and np.isnan(s["lux_clean"].iloc[2])


def test_hampel_removes_isolated_spike_only():
    x = pd.Series(np.r_[np.full(10, 24.0), 39.0, np.full(10, 24.0)] + np.random.default_rng(0).normal(0, 0.05, 21))
    m = clean.hampel_mask(x, 7, 5.0, 0.3)
    assert m.iloc[10] and m.sum() == 1


def test_hampel_keeps_a_real_ramp():
    x = pd.Series(np.linspace(20, 30, 40))
    assert clean.hampel_mask(x, 7, 5.0, 0.3).sum() == 0


def test_dirty_simulation(sim_dirty, cfg0):
    raw, truth = sim_dirty
    ds = clean.normalize(raw, cfg0, raw["ts"].max())
    r, s = ds.report, ds.samples
    assert r["rows_no_ts"] == 3 and r["rows_duplicate"] == 5
    assert r["boot_ids_total"] == 2 and r["gaps"] == 1
    a, b = truth["glitches"]["floating_probe3"]
    win = s.loc[a:b]
    assert len(win) > 100 and (win["m3_state"].isin(["floating", "flagged"])).all()
    assert s["m1_ok"].mean() > 0.99
    assert r["spikes_removed"] >= 3
    ga, gb = ds.gaps[0]
    pl = truth["glitches"]["power_loss"]
    assert abs((ga - pl[0]).total_seconds()) < 3600 and abs((gb - pl[1]).total_seconds()) < 3600


def test_cadence_and_gap_detection(cfg):
    ts = list(pd.date_range("2026-10-20T00:00:00Z", periods=20, freq="15min")) + list(pd.date_range("2026-10-20T09:00:00Z", periods=10, freq="15min"))
    ds = clean.normalize(_df([_row(t) for t in ts]), cfg, NOW)
    assert ds.cadence_s == pytest.approx(900)
    assert len(ds.gaps) == 1
    assert ds.gaps[0] == (pd.Timestamp("2026-10-20T04:45:00Z"), pd.Timestamp("2026-10-20T09:00:00Z"))


def test_sequence_loss_and_restarts(cfg):
    ts = pd.date_range("2026-10-20T00:00:00Z", periods=10, freq="15min")
    rows = [_row(t) for t in ts]
    df = pd.DataFrame(rows)
    df["boot_id"] = [1] * 5 + [2] * 5
    df["seq"] = [0, 1, 2, 5, 6, 0, 1, 2, 3, 4]                 # seq 3, 4 never arrived
    r = clean.normalize(df, cfg, NOW).report
    assert r["seq_lost_7d"] == 2 and r["restarts_7d"] == 1


def test_dht_stuck_is_reported_not_masked(cfg):
    ts = pd.date_range("2026-10-18T00:00:00Z", periods=60, freq="15min")
    ds = clean.normalize(_df([_row(t) for t in ts]), cfg, pd.Timestamp("2026-10-18T15:00:00Z"))
    assert ds.report["dht_stuck"] is True
    assert ds.samples["air_t_clean"].notna().all()


def test_empty_input(cfg):
    ds = clean.normalize(pd.DataFrame(), cfg, NOW)
    assert ds.samples.empty and ds.report["rows_in"] == 0


def test_grid_regular_and_short_gaps_bridged(cfg):
    from soilmon import derive
    ts = pd.date_range("2026-10-20T00:00:00Z", periods=12, freq="15min").delete([4, 5])
    ds = clean.normalize(_df([_row(t) for t in ts]), cfg, NOW)
    derive.add_derived(ds, cfg)
    g = clean.to_grid(ds, cfg)
    assert len(g) == 12 and (g.index.to_series().diff().dropna() == pd.Timedelta(minutes=15)).all()
    assert g["n"].iloc[4] == 0 and g["w_med"].notna().all()     # bridged because the gap is under max_interp_h
