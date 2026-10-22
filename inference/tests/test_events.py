import numpy as np
import pandas as pd
import pytest
from conftest import blank_grid, set_w

from soilmon import clean, derive, events as ev


def _grid_from_sim(sim, cfg):
    raw, truth = sim
    ds = clean.normalize(raw, cfg, raw["ts"].max())
    derive.add_derived(ds, cfg)
    return clean.to_grid(ds, cfg), truth


def test_detects_every_simulated_watering(sim_dirty, cfg0):
    g, truth = _grid_from_sim(sim_dirty, cfg0)
    found = ev.detect_events(g, cfg0)
    assert len(found) == len(truth["events"])
    for f, t in zip(found, truth["events"]):
        assert abs((f["t_detect"] - t).total_seconds()) <= 1800
        assert f["confidence"] == "high" and f["amplitude"] > 0.3


def test_no_false_events_on_a_dry_down(sim_clean, cfg0):
    g, truth = _grid_from_sim(sim_clean, cfg0)
    found = ev.detect_events(g, cfg0)
    assert len(found) == len(truth["events"])


def _flat(cfg, n=96, level=0.5):
    g = blank_grid(steps=n)
    return set_w(g, np.full(n, level))


def test_flat_noise_gives_no_events(cfg0):
    rng = np.random.default_rng(0)
    g = blank_grid(steps=200)
    set_w(g, m1=0.5 + rng.normal(0, 0.01, 200), m2=0.5 + rng.normal(0, 0.01, 200), m3=0.5 + rng.normal(0, 0.01, 200))
    assert ev.detect_events(g, cfg0) == []


def test_one_probe_jumping_is_not_a_watering(cfg0):
    n = 96
    w = np.full(n, 0.4)
    j = w.copy()
    j[40:] = 0.8
    g = set_w(blank_grid(steps=n), m1=j, m2=w, m3=w)
    assert ev.detect_events(g, cfg0) == []


def test_single_sample_spike_is_rejected(cfg0):
    n = 96
    w = np.full(n, 0.4)
    s = w.copy()
    s[40] = 0.9
    g = set_w(blank_grid(steps=n), m1=s, m2=s, m3=s)
    assert ev.detect_events(g, cfg0) == []


def test_two_rises_inside_the_refractory_period_are_one_event(cfg0):
    n = 96
    w = np.full(n, 0.3)
    w[30:] = 0.6
    w[34:] = 0.9                                              # second step 1 h after the first
    g = set_w(blank_grid(steps=n), w)
    found = ev.detect_events(g, cfg0)
    assert len(found) == 1 and found[0]["w_peak"] == pytest.approx(0.9)


def test_two_waterings_far_apart_are_two_events(cfg0):
    n = 200
    w = np.full(n, 0.3)
    w[30:] = 0.9
    w[100:] = 0.3
    w[140:] = 0.9
    g = set_w(blank_grid(steps=n), w)
    assert len(ev.detect_events(g, cfg0)) == 2


def test_one_working_probe_is_low_confidence_and_needs_a_bigger_rise(cfg0):
    n = 96
    w = np.full(n, 0.3)
    w[30:] = 0.5                                              # 0.2 rise: less than 1.5 x 0.10 ... passes
    g = set_w(blank_grid(steps=n), m1=w, m2=np.full(n, np.nan), m3=np.full(n, np.nan))
    f = ev.detect_events(g, cfg0)
    assert len(f) == 1 and f[0]["confidence"] == "low"
    w2 = np.full(n, 0.3)
    w2[30:] = 0.4                                              # 0.10 rise, one probe: rejected
    g2 = set_w(blank_grid(steps=n), m1=w2, m2=np.full(n, np.nan), m3=np.full(n, np.nan))
    assert ev.detect_events(g2, cfg0) == []


def test_manual_log_parsing_and_timezone(tmp_path):
    p = tmp_path / "log.csv"
    p.write_text("# comment\ntimestamp_local,type,volume_ml,note\n2026-10-12 12:30,water,250,first\n2026-10-13 09:00,fertilize,,half dose\nnot a date,water,,\n")
    df = ev.load_manual_log(p, "Asia/Kolkata")
    assert len(df) == 2
    assert df["ts"].iloc[0] == pd.Timestamp("2026-10-12T07:00:00Z")           # IST = UTC+5:30
    assert list(df["type"]) == ["water", "fertilize"] and df["volume_ml"].iloc[0] == 250


def test_manual_log_missing_or_header_only(tmp_path):
    assert ev.load_manual_log(tmp_path / "nope.csv", "Asia/Kolkata").empty
    p = tmp_path / "h.csv"
    p.write_text("timestamp_local,type,volume_ml,note\n")
    assert ev.load_manual_log(p, "Asia/Kolkata").empty


def test_merge_matches_missed_and_unlogged(sim_dirty, cfg0):
    g, truth = _grid_from_sim(sim_dirty, cfg0)
    found = ev.detect_events(g, cfg0)
    t = truth["events"]
    manual = pd.DataFrame({
        "ts": [t[0] + pd.Timedelta(minutes=20), t[1] - pd.Timedelta(hours=1), t[2] + pd.Timedelta(days=1)],   # 2 matches, 1 the detector never saw
        "type": ["water", "water", "water"], "volume_ml": [200.0, np.nan, np.nan], "note": ["", "", ""]})
    events, val = ev.merge_with_manual(found, manual, cfg0, (g.index.min(), g.index.max()))
    assert val["logged"] == 3 and val["matched"] == 2 and val["missed"] == 1 and val["unlogged"] == len(found) - 2
    assert val["recall"] == pytest.approx(2 / 3) and val["precision"] == pytest.approx(2 / len(found))
    assert sorted(e["source"] for e in events).count("both") == 2
    assert sum(e["source"] == "manual" for e in events) == 1
    assert [e["t_detect"] for e in events] == sorted(e["t_detect"] for e in events)


def test_log_outside_data_span_is_not_counted_as_missed(cfg0):
    g = _flat(cfg0)
    manual = pd.DataFrame({"ts": [pd.Timestamp("2025-01-01T00:00:00Z")], "type": ["water"], "volume_ml": [np.nan], "note": [""]})
    _, val = ev.merge_with_manual([], manual, cfg0, (g.index.min(), g.index.max()))
    assert val["logged"] == 0 and val["recall"] is None
