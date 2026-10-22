import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from soilmon import fetch, run
from soilmon.config import load_config
from soilmon.export import write_all
from soilmon.pipeline import run_pipeline
from soilmon.simulator import simulate

ROOT = Path(__file__).resolve().parents[2]


def _strict_load(path):
    def bad(x):
        raise ValueError(f"non-finite literal {x} in {path}")
    return json.loads(Path(path).read_text(), parse_constant=bad)


def test_full_run_on_dirty_simulation(sim_dirty, cfg0):
    raw, truth = sim_dirty
    res = run_pipeline(raw, cfg0, now=raw["ts"].max())
    assert len(res.events) == len(truth["events"])
    assert res.forecast["status"] in ("ok", "no_trend", "at_or_below", "insufficient")
    assert res.thermal["status"] == "ok" and res.thermal["tau_h"] == pytest.approx(truth["tau_h"], rel=0.2)
    assert res.ds.report["rows_no_ts"] == 3
    assert "uncalibrated" in [f["id"] for f in res.flags]
    sev = [f["sev"] for f in res.flags]
    assert sev == sorted(sev, key=lambda s: -{"ok": 0, "info": 1, "warn": 2, "crit": 3}[s])
    assert set(res.explain) >= {"moisture", "vpd", "ppfd", "events"}


def test_floating_probe_is_flagged_while_it_is_happening(sim_dirty, cfg0):
    raw, truth = sim_dirty
    a, b = truth["glitches"]["floating_probe3"]
    cut = b - pd.Timedelta(hours=1)
    res = run_pipeline(raw[raw["ts"].isna() | (raw["ts"] <= cut)], cfg0, now=cut)
    f = {x["id"]: x for x in res.flags}
    assert f["probe_m3"]["sev"] == "crit" and "floating" in f["probe_m3"]["detail"]
    assert "probe_m1" not in f and "probe_m2" not in f
    assert res.now["w_med"]["v"] is not None                    # median of the two healthy probes still works
    assert res.cards["moisture"]["probes"]["m3"] is None


def test_node_going_silent_raises_stale(sim_clean, cfg0):
    raw, _ = sim_clean
    res = run_pipeline(raw, cfg0, now=raw["ts"].max() + pd.Timedelta(hours=8))
    assert {f["id"]: f["sev"] for f in res.flags}["stale"] == "crit" and res.overall["level"] == "crit"
    res = run_pipeline(raw, cfg0, now=raw["ts"].max() + pd.Timedelta(hours=2))
    assert {f["id"]: f["sev"] for f in res.flags}["stale"] == "warn"


def test_humid_spell_raises_the_flag(cfg0):
    raw, _ = simulate(cfg0, days=8, seed=4, glitches=False, humid_spell=(24, 110))
    res = run_pipeline(raw, cfg0, now=raw["ts"].min() + pd.Timedelta(hours=24 + 100))
    f = {x["id"]: x["sev"] for x in res.flags}
    assert f.get("humid_air") in ("warn", "crit") and res.humid["hours"] > 24


def test_manual_log_validation_in_pipeline(sim_clean, cfg0):
    raw, truth = sim_clean
    manual = pd.DataFrame({"ts": [t + pd.Timedelta(minutes=10) for t in truth["events"]], "type": "water",
                           "volume_ml": 200.0, "note": ""})
    res = run_pipeline(raw, cfg0, now=raw["ts"].max(), manual=manual)
    assert res.validation["recall"] == 1.0 and res.validation["precision"] == 1.0
    assert all(e["source"] == "both" for e in res.events)


def test_empty_input_does_not_crash_and_says_so(cfg0, tmp_path):
    res = run_pipeline(pd.DataFrame(), cfg0, now=pd.Timestamp("2026-10-20T00:00:00Z"))
    assert [f["id"] for f in res.flags] == ["no_data"] and res.overall["level"] == "crit"
    write_all(res, cfg0, tmp_path, "supabase")
    latest = _strict_load(tmp_path / "latest.json")
    assert latest["status"]["level"] == "crit" and _strict_load(tmp_path / "raw.json") == {"t": []}


def test_all_probes_dead_still_runs(sim_clean, cfg0):
    raw, _ = sim_clean
    raw = raw.copy()
    for p in ("m1", "m2", "m3"):
        raw[f"{p}_mv"] = 142.0
    res = run_pipeline(raw, cfg0, now=raw["ts"].max())
    ids = [f["id"] for f in res.flags]
    assert {"probe_m1", "probe_m2", "probe_m3"} <= set(ids) and res.events == [] and res.forecast["status"] == "no_data"


def test_export_schema_and_strict_json(sim_dirty, cfg0, tmp_path):
    raw, _ = sim_dirty
    res = run_pipeline(raw, cfg0, now=raw["ts"].max())
    sizes = write_all(res, cfg0, tmp_path, "simulated", generated_at=raw["ts"].max())
    assert set(sizes) == {"latest.json", "raw.json", "series.json", "events.json", "daily.json"}
    assert sum(sizes.values()) < 3_000_000
    latest = {n: _strict_load(tmp_path / n) for n in sizes}
    lt = latest["latest.json"]
    assert lt["schema"] == 1 and lt["source"] == "simulated" and lt["timezone"] == "Asia/Kolkata"
    assert lt["data_through"].endswith("Z") and lt["status"]["level"] in ("ok", "warn", "crit")
    assert lt["forecast"]["status"] and "config" in lt and lt["config"]["w_star"] == 0.4
    for name in ("raw.json", "series.json"):
        d = latest[name]
        n = len(d["t"])
        assert n > 100 and d["t"] == sorted(d["t"])
        for k, v in d.items():
            if isinstance(v, list) and v:
                assert len(v) == n, (name, k)
    assert latest["raw.json"]["m3_state"].count(4) > 100           # floating states are exported, not hidden
    assert any(x > 0 for x in latest["raw.json"]["excluded"])     # spikes are kept in raw but marked as excluded
    ev = latest["events.json"]
    assert len(ev["events"]) == 4 and all(e["source"] == "detected" for e in ev["events"])
    dd = latest["daily.json"]
    assert any(r["complete"] for r in dd["daily"]) and len(dd["heatmap"]["z"]) == 24


def test_export_window_is_limited(cfg0, tmp_path):
    cfg = load_config(ROOT / "config")
    cfg.th.export.raw_days = 5
    cfg.th.export.series_days = 7
    raw, _ = simulate(cfg, days=21, seed=3, glitches=False)
    res = run_pipeline(raw, cfg, now=raw["ts"].max())
    write_all(res, cfg, tmp_path, "simulated")
    raw_j, ser_j = _strict_load(tmp_path / "raw.json"), _strict_load(tmp_path / "series.json")
    assert 5 * 96 <= len(raw_j["t"]) <= 5 * 96 + 2 and 7 * 96 <= len(ser_j["t"]) <= 7 * 96 + 2
    assert len(res.daily) == 22 or len(res.daily) == 21        # all history still summarised


def test_csv_roundtrip_gives_the_same_events(sim_dirty, cfg0, tmp_path):
    raw, _ = sim_dirty
    p = tmp_path / "readings.csv"
    raw.to_csv(p, index=False)
    a = run_pipeline(raw, cfg0, now=raw["ts"].max())
    b = run_pipeline(fetch.load_csv(str(p)), cfg0, now=raw["ts"].max())
    assert [e["t_detect"] for e in a.events] == [e["t_detect"] for e in b.events]


def test_supabase_style_string_timestamps(sim_clean, cfg0):
    raw, _ = sim_clean
    r2 = raw.copy()
    for c in ("ts", "created_at"):
        r2[c] = r2[c].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    r2 = r2.astype(object).where(r2.notna(), None)
    a = run_pipeline(raw, cfg0, now=raw["ts"].max())
    b = run_pipeline(r2, cfg0, now=raw["ts"].max())
    assert len(a.events) == len(b.events) == 4


# ---------------------------------------------------------------------------- fetch
class _Resp:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code, self._p, self.headers, self.text = status, payload, headers or {}, text

    def json(self):
        return self._p


class _Session:
    def __init__(self, rows, page, status=200):
        self.rows, self.page, self.status, self.calls = rows, page, status, []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(headers), dict(params)))
        if self.status != 200:
            return _Resp(self.status, text='{"code":"42501"}')
        if params.get("ts") == "is.null":
            return _Resp(200, [], {"Content-Range": "*/7"})
        off = params["offset"]
        return _Resp(200, self.rows[off: off + params["limit"]])


def test_fetch_paginates_and_counts_null_ts():
    rows = [{"seq": i} for i in range(2500)]
    s = _Session(rows, 1000)
    df, extra = fetch.fetch_supabase("https://x.supabase.co", "sb_secret_abc", "readings", "node-01",
                                     pd.Timestamp("2026-01-01T00:00:00Z"), session=s)
    assert len(df) == 2500 and extra["rows_null_ts"] == 7
    pages = [c for c in s.calls if c[2].get("ts") != "is.null"]
    assert [c[2]["offset"] for c in pages] == [0, 1000, 2000]
    url, headers, params = pages[0]
    assert url == "https://x.supabase.co/rest/v1/readings" and headers["apikey"] == "sb_secret_abc"
    assert "Authorization" not in headers                       # sb_secret_ keys are not JWTs
    assert params["device_id"] == "eq.node-01" and params["ts"].startswith("gte.2026-01-01")


def test_fetch_legacy_jwt_key_uses_bearer():
    s = _Session([], 1000)
    fetch.fetch_supabase("https://x.supabase.co/", "aaa.bbb.ccc", session=s)
    assert s.calls[0][1]["Authorization"] == "Bearer aaa.bbb.ccc"


def test_fetch_errors_are_explained():
    with pytest.raises(fetch.FetchError, match="grant select"):
        fetch.fetch_supabase("https://x.supabase.co", "k", session=_Session([], 1000, status=401))
    with pytest.raises(fetch.FetchError, match="must be set"):
        fetch.fetch_supabase("", "")


# ---------------------------------------------------------------------------- CLI
def test_cli_sim(tmp_path, capsys):
    assert run.main(["--source", "sim", "--days", "14", "--config-dir", str(ROOT / "config"), "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "source=simulated" in out and (tmp_path / "latest.json").exists()
    assert _strict_load(tmp_path / "latest.json")["source"] == "simulated"


def test_cli_csv_and_missing_env(tmp_path, monkeypatch, sim_clean):
    raw, _ = sim_clean
    csv = tmp_path / "r.csv"
    raw.to_csv(csv, index=False)
    assert run.main(["--source", "csv", "--csv", str(csv), "--now", "2026-10-22T00:00:00", "--config-dir", str(ROOT / "config"), "--out", str(tmp_path / "o")]) == 0
    assert run.main(["--source", "csv", "--config-dir", str(ROOT / "config"), "--out", str(tmp_path / "o2")]) == 2
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    assert run.main(["--source", "supabase", "--config-dir", str(ROOT / "config"), "--out", str(tmp_path / "o3")]) == 2
    assert not (tmp_path / "o3" / "latest.json").exists()      # a failed fetch must never publish an empty snapshot
