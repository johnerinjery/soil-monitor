"""I2: dry-down fit (VPD-scaled) and a forecast *window* for when the index crosses W*.  I5: drying anomaly."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import theilslopes

from .clean import PROBES


def _widen(s, lo, hi, resid, floor_rel=0.10):
    """Theil-Sen intervals assume independent errors; 15-min soil data is strongly autocorrelated, so the raw
    interval is far too tight.  Scale its half-width by sqrt(n / n_eff) with n_eff = n (1 - r1) / (1 + r1),
    and never let it be narrower than floor_rel x |slope| (the straight-line model itself is approximate)."""
    r = np.asarray(resid, dtype=float)
    r = r - r.mean()
    den = float(np.sum(r * r))
    r1 = float(np.sum(r[1:] * r[:-1]) / den) if den > 0 and len(r) > 2 else 0.0
    r1 = min(max(r1, 0.0), 0.98)
    scale = min(np.sqrt((1 + r1) / (1 - r1)), 10.0)
    half = max((hi - lo) / 2.0 * scale, floor_rel * abs(s))
    return s - half, s + half


def _anchor_time(ev: dict) -> pd.Timestamp:
    return ev["t_peak"]


def _segment(grid: pd.DataFrame, t0: pd.Timestamp, t1: pd.Timestamp, cfg) -> pd.DataFrame:
    g = grid.loc[t0:t1, ["w_med", "vpd"]].copy()
    g = g[g["w_med"].notna()]
    g = g[(g["w_med"] > 0.02) & (g["w_med"] < 0.99)]          # drop the clipped ends (saturated / bone dry)
    return g


def fit_segment(seg: pd.DataFrame, cfg) -> dict | None:
    """Theil-Sen of W against cumulative VPD exposure [kPa day].  Falls back to plain time if VPD is patchy."""
    f = cfg.th.forecast
    step_d = cfg.th.general.grid_min / 1440.0
    if len(seg) < f.min_points:
        return None
    span_h = (seg.index[-1] - seg.index[0]).total_seconds() / 3600.0
    if span_h < f.min_span_h:
        return None
    vcov = float(seg["vpd"].notna().mean())
    vpd = seg["vpd"].fillna(seg["vpd"].mean()) if vcov >= 0.5 else pd.Series(1.0, index=seg.index)
    x = (vpd.clip(lower=0) * step_d).cumsum().to_numpy()
    xt = ((seg.index - seg.index[0]).total_seconds() / 86400.0).to_numpy()
    y = seg["w_med"].to_numpy()
    if len(y) > 600:                                      # Theil-Sen is O(n^2): thin it, keep the shape
        k = np.linspace(0, len(y) - 1, 600).astype(int)
        xs, ys, xts = x[k], y[k], xt[k]
    else:
        xs, ys, xts = x, y, xt
    if np.ptp(xs) <= 0:
        return None
    s, b, lo, hi = theilslopes(ys, xs, 0.95)
    st, bt, lot, hit = theilslopes(ys, xts, 0.95)
    resid = ys - (b + s * xs)
    lo, hi = _widen(s, lo, hi, resid)
    lot, hit = _widen(st, lot, hit, ys - (bt + st * xts))
    return {
        "vpd_scaled": bool(vcov >= 0.5), "vpd_coverage": round(vcov, 3),
        "k": -float(s), "k_lo": -float(hi), "k_hi": -float(lo),          # drying coefficient [W per kPa day]
        "rate_per_day": -float(st), "rate_lo": -float(hit), "rate_hi": -float(lot),
        "n": int(len(seg)), "span_h": round(span_h, 1),
        "resid_mad": round(float(np.median(np.abs(resid - np.median(resid)))), 4),
        "w_first": round(float(y[0]), 3), "w_last": round(float(y[-1]), 3),
        "t0": seg.index[0], "t1": seg.index[-1], "b": float(b), "x": x, "xt": xt,
    }


def dry_down(grid: pd.DataFrame, events: list[dict], cfg, now: pd.Timestamp) -> dict:
    """Current dry-down fit, history of earlier dry-downs, and the crossing window for W*."""
    f, m = cfg.th.forecast, cfg.th.moisture
    res: dict = {"status": "no_data", "w_star": m.target_w}
    if grid.empty or grid["w_med"].notna().sum() == 0:
        return res
    last_ts = grid.index.max()
    anchors = [_anchor_time(e) + pd.Timedelta(hours=f.settle_h) for e in events]
    # history: complete dry-downs between consecutive events
    hist = []
    for i, a in enumerate(anchors[:-1]):
        seg = _segment(grid, a, events[i + 1]["t_start"], cfg)
        fit = fit_segment(seg, cfg)
        if fit:
            hist.append({"t0": fit["t0"], "t1": fit["t1"], "k": round(fit["k"], 3), "rate_per_day": round(fit["rate_per_day"], 3), "n": fit["n"]})
    res["history"] = hist

    t0 = anchors[-1] if anchors else last_ts - pd.Timedelta(days=f.max_segment_days)
    t0 = max(t0, last_ts - pd.Timedelta(days=f.max_segment_days), grid.index.min())
    res["since"] = t0
    res["last_event"] = events[-1]["t_detect"] if events else None
    w_now = float(grid["w_med"].dropna().iloc[-4:].median())
    res["w_now"] = round(w_now, 3)
    seg = _segment(grid, t0, last_ts, cfg)
    fit = fit_segment(seg, cfg)
    if fit is None:
        res["status"] = "insufficient"
        res["note"] = f"need >= {f.min_span_h:g} h of dry-down data after the last watering (have {len(seg) * cfg.th.general.grid_min / 60:.1f} h)"
        return res
    res["fit"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in fit.items() if k not in ("x", "xt")}
    if fit["k"] <= 0 or fit["k_lo"] <= 0:                  # 95 % range includes zero: no demonstrable trend
        res["status"] = "no_trend"
        res["note"] = "no downward trend in the index since the last watering"
        return res
    if w_now <= m.target_w:
        res["status"] = "at_or_below"
        return res

    # expected evaporative demand over the coming days = recent mean VPD
    lb = grid["vpd"].loc[last_ts - pd.Timedelta(days=f.vpd_lookback_days):].dropna()
    vbar = float(lb.mean()) if len(lb) else 1.0
    daily = lb.resample("1D").mean().dropna()
    sig = float(daily.std()) if len(daily) >= 2 else 0.0
    sig = max(sig, 0.1)
    if not fit["vpd_scaled"]:
        vbar, sig = 1.0, 0.0
    vbar = max(vbar, f.min_vpd_kpa)
    dw = w_now - m.target_w
    k, klo, khi = fit["k"], max(fit["k_lo"], 0.0), fit["k_hi"]
    mid = dw / (k * vbar) * 24.0
    t_lo = dw / (khi * (vbar + sig)) * 24.0
    t_hi = dw / (klo * max(vbar - sig, f.min_vpd_kpa)) * 24.0 if klo > 1e-6 else None
    res.update({"status": "ok", "vpd_expected": round(vbar, 3), "vpd_sigma": round(sig, 3),
                "hours_mid": round(mid, 1), "hours_lo": round(t_lo, 1),
                "hours_hi": None if t_hi is None else round(t_hi, 1),
                "t_mid": now + pd.Timedelta(hours=mid), "t_lo": now + pd.Timedelta(hours=t_lo),
                "t_hi": None if t_hi is None else now + pd.Timedelta(hours=t_hi)})
    # projection curve for the chart: next 7 days at the expected VPD
    hrs = np.arange(0, 7 * 24 + 1, 3)
    cur = lambda kk, vv: np.clip(w_now - kk * vv * hrs / 24.0, 0, 1)
    res["projection"] = {
        "t": [now + pd.Timedelta(hours=float(h)) for h in hrs],
        "mid": cur(k, vbar).round(3).tolist(),
        "lo": cur(khi, vbar + sig).round(3).tolist(),
        "hi": cur(klo, max(vbar - sig, f.min_vpd_kpa)).round(3).tolist(),
    }
    # fitted line over the segment (for the chart)
    t = fit["t0"] + pd.to_timedelta(fit["xt"], unit="D")
    wline = fit["b"] - (fit["k"] * fit["x"])
    k_idx = np.linspace(0, len(t) - 1, min(len(t), 120)).astype(int)
    res["fit_line"] = {"t": [t[i] for i in k_idx], "w": [round(float(wline[i]), 3) for i in k_idx]}
    return res


def drying_anomaly(grid: pd.DataFrame, events: list[dict], cfg) -> dict:
    """k_roll = (W drop over 6 h) / (VPD exposure over those 6 h).  Flag when it exceeds factor x its own median."""
    a, g = cfg.th.drying_anomaly, cfg.th.general
    out = {"status": "no_data"}
    if grid.empty:
        return out
    n = max(2, int(round(a.window_h * 60 / g.grid_min)))
    step_d = g.grid_min / 1440.0
    w = grid["w_med"]
    drop = w.shift(n) - w
    expo = (grid["vpd"].clip(lower=0) * step_d).rolling(n, min_periods=int(n * 0.8)).sum()
    k = (drop / expo).where(expo > 0.02)
    # exclude windows that contain or follow a watering
    bad = pd.Series(False, index=grid.index)
    for e in events:
        bad.loc[e["t_start"]:e["t_peak"] + pd.Timedelta(hours=cfg.th.forecast.settle_h + a.window_h)] = True
    k = k.mask(bad | (w < 0.05) | (w.shift(n) > 0.99))
    valid = k.dropna()
    out["n_valid"] = int(len(valid))
    if len(valid) < a.min_baseline_points:
        out["status"] = "insufficient"
        return out
    hist = valid.iloc[:-n] if len(valid) > 2 * n else valid
    base = float(hist.median())
    out["baseline_k"] = round(base, 3)
    if base <= 0:
        out["status"] = "no_baseline"
        return out
    thr = a.factor * base
    recent = k.iloc[-n:]
    run = int((recent > thr).iloc[::-1].cumprod().sum()) if recent.notna().any() else 0
    out.update({"status": "anomaly" if run >= n else "ok", "k_now": None if pd.isna(k.dropna().iloc[-1]) else round(float(k.dropna().iloc[-1]), 3),
                "threshold": round(thr, 3), "run_steps": run})
    out["series"] = k
    return out
