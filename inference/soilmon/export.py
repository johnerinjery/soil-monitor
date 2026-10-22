"""Write the JSON snapshot the static dashboard reads.  No NaN literals, UTC epoch seconds for time axes."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .clean import PROBES
from .config import unbox

STATE_CODES = {"ok": 0, "noisy": 1, "flagged": 2, "open": 3, "floating": 4, "null": 5}


def _ep(t) -> int | None:
    return None if t is None or pd.isna(t) else int(pd.Timestamp(t).timestamp())


def sanitize(o):
    """Recursively make an object JSON-safe: Timestamps -> ISO strings, NaN/inf -> None, numpy -> python."""
    if isinstance(o, dict):
        return {str(k): sanitize(v) for k, v in o.items() if not isinstance(v, (pd.Series, pd.DataFrame))}
    if isinstance(o, (list, tuple)):
        return [sanitize(v) for v in o]
    if isinstance(o, (pd.Timestamp, datetime)):
        return None if pd.isna(o) else pd.Timestamp(o).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if (math.isnan(o) or math.isinf(o)) else float(o)
    if o is pd.NaT:
        return None
    return o


def _col(s: pd.Series, nd: int) -> list:
    return [None if pd.isna(v) else round(float(v), nd) for v in s.to_numpy()]


def _write(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(sanitize(obj), separators=(",", ":"), allow_nan=False)
    path.write_text(txt, encoding="utf-8")
    return len(txt)


def build_raw(res, cfg) -> dict:
    df = res.ds.samples
    if df.empty:
        return {"t": []}
    df = df.loc[df.index.max() - pd.Timedelta(days=cfg.th.export.raw_days):]
    out = {"t": [_ep(t) for t in df.index]}
    for p in PROBES:
        out[f"{p}_mv"] = _col(df[f"{p}_mv"], 0)
        out[f"{p}_mad"] = _col(df[f"{p}_mad"], 0)
        out[f"{p}_state"] = [STATE_CODES.get(s, 5) for s in df[f"{p}_state"]]
    for name, c in (("soil_t", "soil_t_c"), ("air_t", "air_t_c"), ("rh", "rh_pct"), ("lux", "lux")):
        out[name] = _col(df[c], 2 if name != "lux" else 1)
    for c in ("vbat_mv", "rssi"):
        out[c] = _col(df[c], 0)
    out["flags"] = [int(x) if not pd.isna(x) else 0 for x in df["flags"]]
    # bit mask of raw values that exist but were left out of the analysis (spike filter / invalid flag)
    excl = ((df["soil_t_c"].notna() & df["soil_t_clean"].isna()).astype(int)
            + 2 * (df["air_t_c"].notna() & df["air_t_clean"].isna()).astype(int)
            + 4 * (df["rh_pct"].notna() & df["rh_clean"].isna()).astype(int)
            + 8 * (df["lux"].notna() & df["lux_clean"].isna()).astype(int))
    out["excluded"] = excl.tolist()
    return out


def build_series(res, cfg) -> dict:
    g = res.grid
    if g.empty:
        return {"t": []}
    g = g.loc[g.index.max() - pd.Timedelta(days=cfg.th.export.series_days):]
    out = {"t": [_ep(t) for t in g.index], "step_s": int(cfg.th.general.grid_min * 60)}
    spec = {"w_m1": 3, "w_m2": 3, "w_m3": 3, "w_med": 3, "w_spread": 3, "vpd": 3, "vpd_sigma": 3, "dew": 2, "margin": 2,
            "ppfd": 2, "soil_t_clean": 2, "air_t_clean": 2, "rh_clean": 1, "lux_clean": 1}
    for c, nd in spec.items():
        out[c.replace("_clean", "")] = _col(g[c], nd)
    hs = res.humid.get("series")
    out["humid_h"] = _col(hs.reindex(g.index), 2) if hs is not None else []
    ks = res.anomaly.get("series")
    out["k_roll"] = _col(ks.reindex(g.index), 3) if ks is not None else []
    return out


def build_latest(res, cfg, source: str, generated_at: pd.Timestamp) -> dict:
    ds, rep = res.ds, res.ds.report
    fc = dict(res.forecast)
    for k in ("t_mid", "t_lo", "t_hi", "since", "last_event"):
        if k in fc:
            fc[k] = _ep(fc[k])
    if "projection" in fc:
        fc["projection"] = {**fc["projection"], "t": [_ep(t) for t in fc["projection"]["t"]]}
    if "fit_line" in fc:
        fc["fit_line"] = {**fc["fit_line"], "t": [_ep(t) for t in fc["fit_line"]["t"]]}
    if "fit" in fc:
        fc["fit"] = {k: (_ep(v) if k in ("t0", "t1") else v) for k, v in fc["fit"].items()}
    fc["history"] = [{**h, "t0": _ep(h["t0"]), "t1": _ep(h["t1"])} for h in fc.get("history", [])]
    th, cal = cfg.th, cfg.cal
    last = ds.samples.index.max() if not ds.samples.empty else None
    gaps = [(_ep(a), _ep(b)) for a, b in ds.gaps if last is not None and b >= last - pd.Timedelta(days=th.export.series_days)]
    return {
        "schema": 1, "version": __version__, "source": source,
        "generated_at": generated_at, "data_through": last, "data_from": ds.samples.index.min() if not ds.samples.empty else None,
        "timezone": cfg.tz, "device_id": cal.site.device_id, "cadence_s": rep.get("cadence_s"),
        "status": res.overall, "flags": res.flags,
        "now": res.now,
        "cards": {
            **res.cards,
            "vpd": res.vpd, "overwet": res.overwet,
            "humid": {k: v for k, v in res.humid.items() if k != "series"},
            "light": res.light, "thermal": res.thermal, "condensation": res.cond, "comfort": res.comfort,
            "anomaly": {k: v for k, v in res.anomaly.items() if k != "series"},
        },
        "forecast": fc, "quality": res.quality, "health": {k: v for k, v in rep.items()},
        "validation": res.validation, "explain": res.explain, "gaps": gaps,
        "state_codes": STATE_CODES,
        "config": {
            "moisture_calibrated": bool(cal.moisture.calibrated), "w_star": th.moisture.target_w, "w_wet": th.moisture.wet_w,
            "vpd": unbox(th.vpd), "dli": [th.light.dli_low, th.light.dli_high], "ppfd_per_lux": cal.light.ppfd_per_lux,
            "photoperiod_lux": cal.light.photoperiod_lux, "mv_min_valid": cal.moisture.mv_min_valid,
            "mad_max_mv": cal.moisture.mad_max_mv, "comfort": unbox(th.comfort), "condensation": unbox(th.condensation),
            "humid": unbox(th.humid_air), "rh_threshold": th.humid_air.rh_threshold, "overwet": unbox(th.overwet),
            "raw_days": th.export.raw_days, "series_days": th.export.series_days,
        },
    }


def build_events(res, cfg) -> dict:
    evs = []
    for e in res.events:
        evs.append({"t": _ep(e["t_detect"]), "t_start": _ep(e["t_start"]), "t_peak": _ep(e["t_peak"]), "w_start": e["w_start"],
                    "w_peak": e["w_peak"], "amplitude": e["amplitude"], "n_probes": e["n_probes"], "n_valid": e["n_valid"],
                    "confidence": e["confidence"], "source": e["source"], "volume_ml": e.get("volume_ml")})
    other = []
    if res.manual is not None and len(res.manual):
        for _, r in res.manual.iterrows():
            if r["type"] != "water":
                other.append({"t": _ep(r["ts"]), "type": r["type"], "note": r["note"]})
    return {"events": evs, "other_log": other, "validation": res.validation}


def write_all(res, cfg, out_dir: str | Path, source: str, generated_at: pd.Timestamp | None = None) -> dict:
    out = Path(out_dir)
    gen = pd.Timestamp.now(tz="UTC") if generated_at is None else generated_at
    sizes = {
        "latest.json": _write(out / "latest.json", build_latest(res, cfg, source, gen)),
        "raw.json": _write(out / "raw.json", build_raw(res, cfg)),
        "series.json": _write(out / "series.json", build_series(res, cfg)),
        "events.json": _write(out / "events.json", build_events(res, cfg)),
    }
    daily = res.daily.to_dict("records") if res.daily is not None and len(res.daily) else []
    heat = res.heat
    keep = cfg.th.export.series_days
    if heat.get("dates"):
        heat = {"dates": heat["dates"][-keep:], "hours": heat["hours"], "z": [row[-keep:] for row in heat["z"]]}
    sizes["daily.json"] = _write(out / "daily.json", {"daily": daily, "heatmap": heat})
    return sizes
