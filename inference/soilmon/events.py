"""I1: watering-event detector on the per-probe moisture index, plus the manual watering log."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .clean import PROBES


def load_manual_log(path, tz: str) -> pd.DataFrame:
    """CSV with a local-time column; '#' lines are comments. Returns UTC timestamps, type, volume_ml, note."""
    p = Path(path)
    cols = ["ts", "type", "volume_ml", "note"]
    if not p.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(p, comment="#", skip_blank_lines=True, dtype=str)
    if df.empty or "timestamp_local" not in df.columns:
        return pd.DataFrame(columns=cols)
    loc = pd.to_datetime(df["timestamp_local"].str.strip(), errors="coerce")
    ok = loc.notna()
    out = pd.DataFrame({
        "ts": loc[ok].dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC"),
        "type": df.get("type", pd.Series("water", index=df.index))[ok].fillna("water").str.strip().str.lower(),
        "volume_ml": pd.to_numeric(df.get("volume_ml"), errors="coerce")[ok],
        "note": df.get("note", pd.Series("", index=df.index))[ok].fillna(""),
    })
    return out.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)[cols]


def detect_events(grid: pd.DataFrame, cfg) -> list[dict]:
    """Watering = W rises >= rise_w within rise_window_h in at least min_probe_fraction of the valid probes.

    A candidate must persist (the median index stays above start + half the threshold for the next 30 min),
    which rejects one-sample jumps from a loose wire.  After an event, `refractory_h` hours are skipped.
    """
    ev = cfg.th.events
    if grid.empty:
        return []
    step_min = cfg.th.general.grid_min
    win = max(2, int(round(ev.rise_window_h * 60 / step_min)))
    ref = int(round(ev.refractory_h * 60 / step_min))
    pk = max(1, int(round(ev.peak_window_h * 60 / step_min)))
    cols = [f"w_{p}" for p in PROBES]
    W = grid[cols]
    rise = W - W.rolling(win + 1, min_periods=2).min()
    n_valid = W.notna().sum(axis=1)
    count = (rise >= ev.rise_w).sum(axis=1)
    need = np.maximum(1, np.ceil(ev.min_probe_fraction * n_valid - 1e-9))
    cand = (n_valid >= 1) & (count >= need)
    wmed = grid["w_med"]
    out, last = [], -10 ** 9
    idx = grid.index
    for i in np.flatnonzero(cand.to_numpy()):
        if i - last < ref:
            continue
        lo = max(0, i - win)
        seg = wmed.iloc[lo:i + 1]
        if seg.notna().sum() < 2:
            continue
        i0 = lo + int(np.nanargmin(seg.to_numpy()))
        w0 = float(wmed.iloc[i0])
        if n_valid.iloc[i] < 2 and wmed.iloc[i] - w0 < 1.5 * ev.rise_w:
            continue
        after = wmed.iloc[i:i + 3].dropna()
        if len(after) >= 2 and after.median() < w0 + 0.5 * ev.rise_w:
            continue
        j1 = min(len(grid), i + pk + 1)
        peak_seg = wmed.iloc[i:j1]
        if peak_seg.notna().sum() == 0:
            continue
        ip = i + int(np.nanargmax(peak_seg.to_numpy()))
        probes_pk = {p: _r(W[f"w_{p}"].iloc[i:j1].max()) for p in PROBES}
        out.append({
            "t_start": idx[i0], "t_detect": idx[i], "t_peak": idx[ip],
            "w_start": _r(w0), "w_peak": _r(wmed.iloc[ip]), "amplitude": _r(wmed.iloc[ip] - w0),
            "n_probes": int(count.iloc[i]), "n_valid": int(n_valid.iloc[i]),
            "confidence": "high" if (n_valid.iloc[i] >= 2 and count.iloc[i] >= 2) else "low",
            "probe_peaks": probes_pk, "source": "detected",
        })
        last = i
    return out


def _r(x, nd=3):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) or pd.isna(x) else round(float(x), nd)


def merge_with_manual(detected: list[dict], manual: pd.DataFrame, cfg, span: tuple | None = None):
    """Match detections to logged waterings (+-match_tolerance_h).

    Returns (events, validation): events carry source detected | manual | both, sorted by time.
    """
    tol = pd.Timedelta(hours=cfg.th.events.match_tolerance_h)
    water = manual[manual["type"] == "water"] if len(manual) else manual
    used, events = set(), []
    for d in detected:
        hit = None
        for k, row in water.iterrows():
            if k in used:
                continue
            if abs(row["ts"] - d["t_detect"]) <= tol or abs(row["ts"] - d["t_start"]) <= tol:
                hit = (k, row)
                break
        e = dict(d)
        if hit is not None:
            used.add(hit[0])
            e["source"] = "both"
            e["manual_ts"] = hit[1]["ts"]
            e["volume_ml"] = _r(hit[1]["volume_ml"], 0)
        events.append(e)
    n_missed = 0
    for k, row in water.iterrows():
        if k in used:
            continue
        if span and not (span[0] - tol <= row["ts"] <= span[1] + tol):
            continue                                     # logged outside the data span: nothing to compare
        n_missed += 1
        events.append({"t_start": row["ts"], "t_detect": row["ts"], "t_peak": row["ts"] + pd.Timedelta(hours=1),
                       "w_start": None, "w_peak": None, "amplitude": None, "n_probes": 0, "n_valid": 0,
                       "confidence": "logged", "probe_peaks": {}, "source": "manual",
                       "volume_ml": _r(row["volume_ml"], 0)})
    events.sort(key=lambda e: e["t_detect"])
    n_det = len(detected)
    n_match = sum(1 for e in events if e["source"] == "both")
    n_log = n_match + n_missed
    val = {
        "logged": n_log, "detected": n_det, "matched": n_match, "missed": n_missed,
        "unlogged": n_det - n_match,
        "recall": (n_match / n_log) if n_log else None,
        "precision": (n_match / n_det) if (n_det and n_log) else None,
    }
    return events, val
