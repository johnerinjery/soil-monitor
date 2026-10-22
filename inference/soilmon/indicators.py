"""I3-I10: over-wet dwell, humid-air exposure, VPD bands, light dose, thermal lag, condensation, comfort, data quality."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import physics as ph
from .clean import PROBES


# ------------------------------------------------------------------ I3
def overwet(grid: pd.DataFrame, cfg) -> dict:
    th, step_h = cfg.th, cfg.th.general.grid_min / 60.0
    out = {"dwell_h": 0.0, "level": "none", "run_h": 0.0, "max_dwell_7d_h": 0.0}
    if grid.empty:
        return out
    w = grid["w_med"]
    wet = pd.Series(np.where(w.notna(), (w >= th.moisture.wet_w).astype(float), np.nan), index=grid.index)
    run = 0
    for v in wet.to_numpy()[::-1]:
        if np.isnan(v):
            if run == 0:
                continue                                 # trailing NaNs: look through to the last valid value
            break
        if v == 1.0:
            run += 1
        else:
            break
    run_h = run * step_h
    dwell = max(0.0, run_h - th.moisture.settle_h)
    out.update(run_h=round(run_h, 2), dwell_h=round(dwell, 2))
    out["level"] = "high" if dwell >= th.overwet.high_h else "elevated" if dwell >= th.overwet.elevated_h else "none"
    last7 = wet.loc[grid.index.max() - pd.Timedelta(days=7):]
    grp = (last7 != last7.shift()).cumsum()
    best = 0.0
    for _, g in last7.groupby(grp):
        if g.iloc[0] == 1:
            best = max(best, len(g) * step_h - th.moisture.settle_h)
    out["max_dwell_7d_h"] = round(max(best, 0.0), 2)
    return out


# ------------------------------------------------------------------ I4
def humid_air(grid: pd.DataFrame, cfg) -> dict:
    """Leaky integrator: +dt while RH >= threshold (5..40 degC), -decay*dt otherwise, never below 0."""
    h, step_h = cfg.th.humid_air, cfg.th.general.grid_min / 60.0
    out = {"hours": 0.0, "level": "none", "rh80_24h_h": 0.0}
    if grid.empty:
        return out
    rh, t = grid["rh_clean"], grid["air_t_clean"]
    known = rh.notna() & t.notna()
    humid = (rh >= h.rh_threshold) & (t >= h.t_min_c) & (t <= h.t_max_c)
    acc, series = 0.0, []
    for k, hm in zip(known.to_numpy(), humid.to_numpy()):
        if k:
            acc = acc + step_h if hm else max(0.0, acc - h.decay * step_h)
        series.append(acc)
    s = pd.Series(series, index=grid.index)
    out["hours"] = round(float(acc), 2)
    out["level"] = "high" if acc >= h.high_h else "elevated" if acc >= h.elevated_h else "none"
    out["rh80_24h_h"] = round(float((rh.loc[grid.index.max() - pd.Timedelta(days=1):] >= h.rh_threshold).sum() * step_h), 2)
    out["series"] = s
    return out


# ------------------------------------------------------------------ I5
def vpd_state(grid: pd.DataFrame, cfg) -> dict:
    v, step_h = cfg.th.vpd, cfg.th.general.grid_min / 60.0
    out = {"now": None, "band": "unknown", "sigma": None}
    if grid.empty or grid["vpd"].notna().sum() == 0:
        return out
    vv = grid["vpd"].dropna()
    now = float(vv.iloc[-3:].mean())

    def band(x):
        return "low" if x < v.low else "ok" if x <= v.ok_max else "warn" if x <= v.warn_max else "high"

    out.update(now=round(now, 3), band=band(now), sigma=round(float(grid["vpd_sigma"].dropna().iloc[-1]), 3))
    last = vv.loc[grid.index.max() - pd.Timedelta(days=1):]
    if len(last):
        bands = last.map(band)
        out["frac_24h"] = {b: round(float((bands == b).mean()), 3) for b in ("low", "ok", "warn", "high")}
        out["max_24h"] = round(float(last.max()), 3)
        out["mean_24h"] = round(float(last.mean()), 3)
    return out


# ------------------------------------------------------------------ I6 daily aggregates
def daily_summary(grid: pd.DataFrame, events: list[dict], cfg) -> tuple[pd.DataFrame, dict]:
    """Per local day: DLI with coverage, photoperiod, VPD, temperatures, humidity, moisture, events.  Plus hour x day PPFD map."""
    lc, th = cfg.cal.light, cfg.th
    if grid.empty:
        return pd.DataFrame(), {"dates": [], "hours": list(range(24)), "z": []}
    tz = cfg.tz
    step_s = th.general.grid_min * 60.0
    step_h = step_s / 3600.0
    g = grid.copy()
    loc = g.index.tz_convert(tz)
    g["date"] = loc.normalize().tz_localize(None)
    g["hour"] = loc.hour
    last_ts = g.index.max()
    rel = math.sqrt(lc.sensor_rel_error ** 2 + lc.source_factor_rel_error ** 2)
    rows = []
    for d, x in g.groupby("date"):
        day_start = x.index.min().tz_convert(tz).normalize()
        day_end = min(day_start + pd.Timedelta(days=1), last_ts.tz_convert(tz) + pd.Timedelta(minutes=th.general.grid_min))
        expected = max(1, int(round((day_end - day_start).total_seconds() / step_s)))
        valid = int(x["ppfd"].notna().sum())
        cov = min(1.0, valid / expected)
        raw = ph.dli_mol(x["ppfd"].to_numpy(), np.full(len(x), step_s))
        full_day = (day_end - day_start) >= pd.Timedelta(hours=23.9)
        complete = bool(full_day and cov >= th.light.min_coverage)
        dli = raw / cov if (complete and cov > 0) else None
        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "dli": None if dli is None else round(dli, 2),
            "dli_so_far": round(raw, 2), "coverage": round(cov, 3), "complete": complete,
            "dli_unc": None if dli is None else round(dli * rel, 2),
            "sat_bins": int(x["lux_sat"].fillna(0).sum()),
            "photoperiod_h": round(float((x["lux_clean"] > lc.photoperiod_lux).sum() * step_h), 2),
            "ppfd_peak": _n(x["ppfd"].max()),
            "vpd_mean": _n(x["vpd"].mean()), "vpd_max": _n(x["vpd"].max()),
            "air_t_min": _n(x["air_t_clean"].min()), "air_t_max": _n(x["air_t_clean"].max()), "air_t_mean": _n(x["air_t_clean"].mean()),
            "soil_t_min": _n(x["soil_t_clean"].min()), "soil_t_max": _n(x["soil_t_clean"].max()),
            "rh_mean": _n(x["rh_clean"].mean()),
            "rh80_h": round(float((x["rh_clean"] >= cfg.th.humid_air.rh_threshold).sum() * step_h), 2),
            "w_mean": _n(x["w_med"].mean(), 3), "w_min": _n(x["w_med"].min(), 3), "w_max": _n(x["w_med"].max(), 3),
            "n_samples": int(x["n"].sum()),
        })
    daily = pd.DataFrame(rows)
    ev_days = pd.Series([e["t_detect"].tz_convert(tz).strftime("%Y-%m-%d") for e in events], dtype=str)
    counts = ev_days.value_counts()
    daily["water_events"] = daily["date"].map(counts).fillna(0).astype(int)

    # hour x day heat map of mean PPFD (rows = hours 0..23, columns = dates)
    hm = g.pivot_table(index="hour", columns="date", values="ppfd", aggfunc="mean").reindex(range(24))
    heat = {"dates": [d.strftime("%Y-%m-%d") for d in hm.columns], "hours": list(range(24)),
            "z": [[None if pd.isna(v) else round(float(v), 2) for v in row] for row in hm.to_numpy()]}
    return daily, heat


def _n(x, nd=2):
    return None if pd.isna(x) else round(float(x), nd)


def light_state(daily: pd.DataFrame, grid: pd.DataFrame, cfg) -> dict:
    th = cfg.th.light
    out = {"status": "no_data"}
    if daily.empty:
        return out
    comp = daily[daily["complete"]]
    out.update(today_so_far=float(daily.iloc[-1]["dli_so_far"]), photoperiod_today_h=float(daily.iloc[-1]["photoperiod_h"]))
    if comp.empty:
        out["status"] = "no_complete_day"
        return out
    y = comp.iloc[-1]
    out.update(last_complete_date=y["date"], dli=float(y["dli"]), dli_unc=float(y["dli_unc"]),
               status="low" if y["dli"] + y["dli_unc"] < th.dli_low else "high" if y["dli"] - y["dli_unc"] > th.dli_high else
               ("ok" if th.dli_low <= y["dli"] <= th.dli_high else "uncertain"))
    out["mean_7d"] = round(float(comp["dli"].tail(7).mean()), 2)
    out["sat_bins_last"] = int(y["sat_bins"])
    return out


# ------------------------------------------------------------------ I7
def thermal_lag(grid: pd.DataFrame, cfg) -> dict:
    """One-pole model dTs/dt = (Ta - Ts)/tau fitted by regression of 1-h soil-temperature changes on the air-soil difference."""
    t = cfg.th.thermal
    out = {"status": "insufficient"}
    if grid.empty:
        return out
    g = grid.loc[grid.index.max() - pd.Timedelta(days=14):, ["soil_t_clean", "air_t_clean"]].dropna()
    if len(g) < t.min_days * 24 * 60 / cfg.th.general.grid_min:
        return out
    dt_h = cfg.th.general.grid_min / 60.0
    k = max(1, int(round(1.0 / dt_h)))                   # 1-hour differences
    full = grid.loc[g.index.min():g.index.max(), ["soil_t_clean", "air_t_clean"]]
    ts, ta = full["soil_t_clean"].to_numpy(), full["air_t_clean"].to_numpy()
    n = len(ts) - k
    if n <= 0:
        return out
    y = ts[k:] - ts[:-k]
    x = np.zeros(n)
    for j in range(k):                                    # rectangle-rule integral of (Ta - Ts) over the hour
        x += (ta[j:j + n] - ts[j:j + n]) * dt_h
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 48 or np.sum(x[ok] ** 2) == 0:
        return out
    inv_tau = float(np.sum(x[ok] * y[ok]) / np.sum(x[ok] ** 2))
    pred = inv_tau * x[ok]
    ss_res, ss_tot = float(np.sum((y[ok] - pred) ** 2)), float(np.sum((y[ok] - y[ok].mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    out.update(n=int(ok.sum()), r2=round(r2, 3))
    if inv_tau <= 1e-6:
        out["status"] = "no_coupling"
        return out
    tau = 1.0 / inv_tau
    amp, lag = ph.thermal_response(tau)
    dr = grid.loc[grid.index.max() - pd.Timedelta(days=7):]
    loc = dr.index.tz_convert(cfg.tz).normalize()
    rs = dr["soil_t_clean"].groupby(loc).agg(lambda s: s.max() - s.min() if s.notna().sum() > 40 else np.nan)
    ra = dr["air_t_clean"].groupby(loc).agg(lambda s: s.max() - s.min() if s.notna().sum() > 40 else np.nan)
    ratio = float((rs / ra).replace([np.inf], np.nan).median()) if len(rs) else float("nan")
    out.update(status="ok", tau_h=round(tau, 2), amplitude_ratio_24h=round(float(amp), 3), phase_lag_h=round(float(lag), 2),
               observed_range_ratio=None if math.isnan(ratio) else round(ratio, 3),
               delta_now=_n(grid["soil_t_clean"].dropna().iloc[-1] - grid["air_t_clean"].dropna().iloc[-1]) if grid["air_t_clean"].notna().any() else None)
    if tau > t.tau_max_h or tau < t.tau_min_h:
        out["note"] = "time constant outside the plausible range: sensor placement or sun on the pot may dominate"
    return out


# ------------------------------------------------------------------ I9 / I10
def condensation(grid: pd.DataFrame, cfg) -> dict:
    c, step_h = cfg.th.condensation, cfg.th.general.grid_min / 60.0
    out = {"margin_now": None, "level": "unknown"}
    m = grid["margin"].dropna() if not grid.empty else pd.Series(dtype=float)
    if m.empty:
        return out
    now = float(m.iloc[-3:].mean())
    last = m.loc[grid.index.max() - pd.Timedelta(days=1):]
    out.update(margin_now=round(now, 2), margin_min_24h=round(float(last.min()), 2),
               hours_below_warn_24h=round(float((last <= c.warn_c).sum() * step_h), 2),
               level="crit" if now <= c.crit_c else "warn" if now <= c.warn_c else "ok")
    return out


def comfort(grid: pd.DataFrame, cfg) -> dict:
    c, step_h = cfg.th.comfort, cfg.th.general.grid_min / 60.0
    out = {"status": "no_data"}
    if grid.empty:
        return out
    x = grid.loc[grid.index.max() - pd.Timedelta(days=1):]
    t, rh = x["air_t_clean"], x["rh_clean"]
    ok = t.notna() & rh.notna()
    if ok.sum() == 0:
        return out
    inb = (t.between(*c.t_ok) & rh.between(*c.rh_ok))[ok]
    out.update(status="ok", frac_in_range=round(float(inb.mean()), 3),
               cold_h=round(float((t < c.t_cold).sum() * step_h), 2), hot_h=round(float((t > c.t_hot).sum() * step_h), 2))
    return out


# ------------------------------------------------------------------ I8
def quality(ds, grid: pd.DataFrame, events: list[dict], cfg) -> dict:
    th = cfg.th
    q = {"probe_states_24h": ds.report.get("probe_states_24h", {}), "valid_frac_24h": ds.report.get("valid_frac_24h", {})}
    spread = {"flag": False}
    if not grid.empty and grid["w_spread"].notna().any():
        step_h = th.general.grid_min / 60.0
        sp = grid["w_spread"]
        n_run = 0
        for v in (sp > th.moisture.spread_flag).to_numpy()[::-1]:
            if v:
                n_run += 1
            else:
                break
        spread.update(now=_n(sp.dropna().iloc[-1], 3), run_h=round(n_run * step_h, 2),
                      flag=bool(n_run * step_h >= th.moisture.spread_flag_h))
        if spread["flag"]:
            last = grid[[f"w_{p}" for p in PROBES]].iloc[-1]
            med = last.median()
            dev = (last - med).abs()
            spread["outlier"] = dev.idxmax().replace("w_", "") if dev.notna().any() else None
    q["spread"] = spread
    # electrode plateau drift: peak index of each probe after successive waterings
    drift = {}
    for p in PROBES:
        pk = [e["probe_peaks"].get(p) for e in events if e.get("probe_peaks") and e["probe_peaks"].get(p) is not None]
        if len(pk) >= 4:
            change = float(np.median(pk[-2:]) - np.median(pk[:2]))
            drift[p] = {"events": len(pk), "change": round(change, 3), "flag": bool(abs(change) > 0.15)}
    q["plateau_drift"] = drift
    return q
