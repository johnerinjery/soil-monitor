"""Turn the numbers into cards, flags (with severity) and 'show your work' strings for the dashboard."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import physics as ph
from .clean import PROBES

SEV_ORDER = {"ok": 0, "info": 1, "warn": 2, "crit": 3}


def _f(x, nd=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) or pd.isna(x) else round(float(x), nd)


def _last_valid(s: pd.Series):
    v = s.dropna()
    return (v.iloc[-1], v.index[-1]) if len(v) else (np.nan, None)


def now_values(ds, now: pd.Timestamp, max_age_h: float = 1.0) -> dict:
    """Last valid cleaned value of every channel, with its age.

    A channel whose last valid value is older than `max_age_h` *relative to the newest sample* is reported as
    missing (value None): a dead probe must not keep showing the number it had yesterday.  Whether the whole
    node has gone quiet is a separate flag (stale)."""
    df = ds.samples
    out = {}
    spec = {"soil_t": "soil_t_clean", "air_t": "air_t_clean", "rh": "rh_clean", "lux": "lux_clean", "vpd": "vpd", "dew": "dew",
            "ppfd": "ppfd", "w_med": "w_med", "w_m1": "w_m1", "w_m2": "w_m2", "w_m3": "w_m3", "margin": "margin",
            "m1_mv": "m1_mv", "m2_mv": "m2_mv", "m3_mv": "m3_mv"}
    for k, c in spec.items():
        v, t = _last_valid(df[c]) if not df.empty else (np.nan, None)
        if t is not None and (df.index.max() - t) > pd.Timedelta(hours=max_age_h):
            v, t = np.nan, None
        out[k] = {"v": _f(v, 3), "age_min": None if t is None else round((now - t).total_seconds() / 60.0, 1)}
    if not df.empty:
        last = df.iloc[-1]
        out["rssi"] = _f(last["rssi"], 0)
        out["vbat_mv"] = _f(last["vbat_mv"], 0)
    return out


def moisture_card(grid, now_vals, dd, cfg) -> dict:
    m = cfg.th.moisture
    w = now_vals["w_med"]["v"]
    card = {"w": w, "calibrated": bool(cfg.cal.moisture.calibrated), "target": m.target_w, "wet": m.wet_w,
            "probes": {p: now_vals[f"w_{p}"]["v"] for p in PROBES}}
    if w is None:
        card["label"] = "no valid probe"
        return card
    card["label"] = "saturated" if w >= m.wet_w else "moist" if w > m.target_w else "dry" if w > 0.2 else "very dry"
    if not grid.empty:
        s = grid["w_med"].dropna()
        past = s.loc[:s.index[-1] - pd.Timedelta(hours=6)]
        if len(past):
            card["trend_6h"] = _f(float(s.iloc[-1] - past.iloc[-1]), 3)
    return card


def build_flags(res, cfg) -> list[dict]:
    """res is the pipeline result object; returns flags sorted by severity."""
    th, rep = cfg.th, res.ds.report
    flags: list[dict] = []

    def add(fid, sev, title, detail, **kw):
        flags.append({"id": fid, "sev": sev, "title": title, "detail": detail, **kw})

    if res.ds.samples.empty:
        add("no_data", "crit", "No usable data", "The table has no rows with a valid timestamp for this device.")
        return flags
    age = rep["age_h"]
    if age >= th.general.stale_crit_h:
        add("stale", "crit", "Node silent", f"Last upload was {age:.1f} h ago (cadence {rep['cadence_s'] / 60:.0f} min). Check power, Wi-Fi hotspot, power bank auto-off.")
    elif age >= th.general.stale_warn_h:
        add("stale", "warn", "Data is late", f"Last upload was {age:.1f} h ago; the node normally reports every {rep['cadence_s'] / 60:.0f} min.")
    if not cfg.cal.moisture.calibrated:
        add("uncalibrated", "info", "Moisture index uncalibrated",
            "Trends, events and dry-down shape are meaningful; absolute levels (target 0.40, saturated 0.95) are placeholders until the dry/wet anchors in config/calibration.yaml are measured.")

    vf = rep.get("valid_frac_24h", {})
    states = rep.get("probe_states_24h", {})
    for p in PROBES:
        frac = vf.get(p, 0)
        if frac < 0.5:
            st = {k: v for k, v in states.get(p, {}).items() if k != "ok"}
            why = max(st, key=st.get) if st else "no data"
            nice = {"floating": "reads like a floating/shorted pin (< 200 mV)", "open": "reads open-circuit (probe unplugged?)",
                    "noisy": "too noisy (loose wire?)", "flagged": "flagged by the firmware", "null": "no value reported"}.get(why, why)
            add(f"probe_{p}", "crit" if frac == 0 else "warn", f"Probe {p[1]} unusable",
                f"Only {frac * 100:.0f}% of the last 24 h of readings were valid; mostly it {nice}.", probe=p)
    for key, name in (("soil_t", "DS18B20 soil temperature"), ("air_t", "DHT22 air temperature"), ("rh", "DHT22 humidity"), ("lux", "BH1750 light")):
        if vf.get(key, 1) < 0.5:
            add(f"sensor_{key}", "warn", f"{name} not reporting", f"Valid in only {vf.get(key, 0) * 100:.0f}% of the last 24 h.")
    sp = res.quality["spread"]
    if sp.get("flag"):
        add("spread", "warn", "Probes disagree",
            f"Spread between probes has exceeded {th.moisture.spread_flag:.2f} for {sp['run_h']:.1f} h (outlier: probe {str(sp.get('outlier', '?'))[-1]}). Placement, contact or a bad probe.")
    for p, d in res.quality["plateau_drift"].items():
        if d["flag"]:
            add(f"drift_{p}", "warn", f"Probe {p[1]} drifting", f"Its post-watering peak moved by {d['change']:+.2f} over {d['events']} waterings: electrode corrosion or salt build-up.")

    ow = res.overwet
    if ow["level"] != "none":
        add("overwet", "crit" if ow["level"] == "high" else "warn", "Soil staying saturated",
            f"Index has been at or above {th.moisture.wet_w:.2f} for {ow['run_h']:.0f} h ({ow['dwell_h']:.0f} h beyond the {th.moisture.settle_h:.0f} h settling time). Root-zone oxygen drops; check drainage.")
    ha = res.humid
    if ha["level"] != "none":
        add("humid_air", "crit" if ha["level"] == "high" else "warn", "Humid air building up",
            f"Weighted exposure to RH >= {th.humid_air.rh_threshold} % is {ha['hours']:.0f} h. Fungal and mould risk rises; ventilate.")
    v = res.vpd
    if v["band"] == "high":
        add("vpd", "warn", "Air very dry for plants", f"VPD {v['now']:.2f} kPa (above {th.vpd.warn_max}). Expect fast drying and possible leaf stress if this lasts.")
    elif v["band"] == "warn":
        add("vpd", "info", "Evaporative demand elevated", f"VPD {v['now']:.2f} kPa (comfortable up to {th.vpd.ok_max}).")
    elif v["band"] == "low":
        add("vpd", "info", "Air very humid / stagnant", f"VPD {v['now']:.2f} kPa is below {th.vpd.low}: little transpiration or drying.")
    an = res.anomaly
    if an.get("status") == "anomaly":
        add("drying_fast", "warn", "Soil drying faster than usual",
            f"Drying coefficient {an.get('k_now')} vs typical {an.get('baseline_k')} W per kPa-day for over {th.drying_anomaly.window_h:g} h. Heat source, draught, or a probe issue.")
    fc = res.forecast
    if fc["status"] == "ok" and fc["hours_mid"] <= 24:
        add("water_soon", "info", "Watering window approaching", f"Index expected to reach {th.moisture.target_w:.2f} in about {fc['hours_lo']:.0f} to {fc['hours_hi'] if fc['hours_hi'] is not None else 'many'} h.")
    elif fc["status"] == "at_or_below":
        add("water_now", "warn", "Index at or below target", f"Current index {fc['w_now']:.2f} is at or below {th.moisture.target_w:.2f}.")
    lt = res.light
    if lt.get("status") == "low":
        add("light_low", "warn", "Light dose low", f"Yesterday's DLI was {lt['dli']:.1f} +- {lt['dli_unc']:.1f} mol/m2/d, below {th.light.dli_low}.")
    elif lt.get("status") == "high":
        add("light_high", "info", "Light dose high", f"Yesterday's DLI was {lt['dli']:.1f} +- {lt['dli_unc']:.1f} mol/m2/d, above {th.light.dli_high}.")
    if lt.get("sat_bins_last", 0) > 0:
        add("lux_sat", "info", "BH1750 saturating", "Bright periods exceeded the sensor range (~54,600 lx); light dose is a lower bound.")
    c = res.cond
    if c["level"] in ("warn", "crit"):
        add("condensation", "crit" if c["level"] == "crit" else "warn", "Condensation risk",
            f"Soil is only {c['margin_now']:.1f} degC above the dew point (warn at {th.condensation.warn_c}).")

    if res.ds.report.get("restarts_7d", 0) >= th.health.restart_flag_per_week:
        add("restarts", "warn", "Node restarting", f"{rep['restarts_7d']} cold restarts in 7 days: brown-outs or the power bank shutting off.")
    lost, exp = rep.get("seq_lost_7d", 0), max(1, rep.get("seq_expected_7d", 1))
    if 100.0 * lost / exp > th.health.seq_gap_warn_pct:
        add("seq_loss", "warn", "Records lost in transit", f"{lost} of {exp} expected records are missing in the last 7 days (buffer overflow or backlog discarded).")
    rssi = res.now.get("rssi")
    if rssi is not None and rssi < th.health.rssi_weak:
        add("rssi", "info", "Weak Wi-Fi", f"RSSI {rssi:.0f} dBm; uploads may fail or take longer.")
    vb = res.now.get("vbat_mv")
    if vb and vb < th.health.vbat_low_mv:
        add("battery", "warn", "Battery low", f"{vb:.0f} mV.")
    if rep.get("dht_stuck"):
        add("dht_stuck", "warn", "DHT22 may be stuck", f"Air temperature and humidity unchanged for {rep['dht_stuck_run']} samples.")
    if rep.get("rows_no_ts"):
        add("no_ts", "info", "Rows without timestamp", f"{rep['rows_no_ts']} row(s) were uploaded before the clock synced and are left out.")
    val = res.validation
    if val.get("logged", 0) >= 3 and val.get("recall") is not None and val["recall"] < 0.67:
        add("detector", "info", "Watering detector missing events", f"It found {val['matched']} of {val['logged']} logged waterings; lower events.rise_w or check the probes.")
    flags.sort(key=lambda f: -SEV_ORDER[f["sev"]])
    return flags


def overall(flags) -> dict:
    top = max((SEV_ORDER[f["sev"]] for f in flags), default=0)
    sev = {0: "ok", 1: "ok", 2: "warn", 3: "crit"}[top]
    label = {"ok": "All good", "warn": "Needs attention", "crit": "Action needed"}[sev]
    return {"level": sev, "label": label, "n_warn": sum(f["sev"] == "warn" for f in flags), "n_crit": sum(f["sev"] == "crit" for f in flags),
            "n_info": sum(f["sev"] == "info" for f in flags)}


def explain(res, cfg) -> dict:
    """Human-readable derivations using the actual latest numbers."""
    m, a = cfg.cal.moisture, cfg.cal.air
    out, n = {}, res.now
    df = res.ds.samples
    if not df.empty:
        last = df.dropna(subset=["w_med"]).iloc[-1] if df["w_med"].notna().any() else None
        if last is not None:
            lines = []
            for p in PROBES:
                if last[f"{p}_ok"]:
                    v = float(last[f"{p}_mv_clean"])
                    g = float(ph.conductance_s(v, m.vcc_mv, m.r_fixed_ohm))
                    lines.append(f"probe {p[1]}: V = {v:.0f} mV -> G = ({m.vcc_mv:g} - {v:.0f}) / ({m.r_fixed_ohm:g} x {v:.0f}) = {g * 1e6:.1f} uS -> index {last[f'w_{p}']:.2f}")
            out["moisture"] = ("Index W = (G25 - G_dry) / (G_wet - G_dry), with G = (Vcc - V) / (Rf V) and G25 = G / (1 + "
                               f"{m.tc_per_degC:g} (T - 25)), T = {last['t_comp']:.1f} degC. " + "; ".join(lines) +
                               f". Median of valid probes = {last['w_med']:.2f}.")
    v, t, rh = n["vpd"]["v"], n["air_t"]["v"], n["rh"]["v"]
    if None not in (v, t, rh):
        es = float(ph.sat_vapor_pressure_kpa(t))
        out["vpd"] = (f"VPD = es(T) (1 - RH/100), es = 0.6108 exp(17.27 T / (T + 237.3)) = {es:.2f} kPa at {t:.1f} degC; "
                      f"x (1 - {rh:.0f}/100) = {v:.2f} kPa. 1-sigma about {res.vpd.get('sigma')} kPa from DHT22 accuracy (+-{a.dht_sigma_t_c} degC, +-{a.dht_sigma_rh_pct} % RH).")
        if n["dew"]["v"] is not None:
            out["dew"] = f"Dew point (Magnus) = {n['dew']['v']:.1f} degC. Condensation margin = soil temperature - dew point = {n['margin']['v'] if n['margin']['v'] is not None else float('nan'):.1f} degC."
    lx = n["lux"]["v"]
    if lx is not None:
        out["ppfd"] = (f"PPFD = {cfg.cal.light.ppfd_per_lux:g} x {lx:.0f} lx = {lx * cfg.cal.light.ppfd_per_lux:.1f} umol/m2/s. The factor depends on the light source (sun 0.0185, white LED 0.0146, fluorescent 0.0135); "
                       f"with the daylight + lamp mix assumed here, daily light has about +-{math.hypot(cfg.cal.light.sensor_rel_error, cfg.cal.light.source_factor_rel_error) * 100:.0f} % uncertainty.")
        out["dli"] = "DLI = sum(PPFD x dt) x 1e-6 over the local day [mol/m2/d]; days with < 80 % valid light samples are not reported as complete."
    fc = res.forecast
    if fc.get("fit"):
        f = fc["fit"]
        out["forecast"] = (f"Since the last watering the index fell from {f['w_first']:.2f} to {f['w_last']:.2f} over {f['span_h']:.0f} h. "
                           f"Theil-Sen fit of W against cumulative VPD exposure gives k = {f['k']:.3f} (95 % range {f['k_lo']:.3f} to {f['k_hi']:.3f}) W per kPa-day"
                           + (f"; with an expected VPD of {fc['vpd_expected']:.2f} +- {fc['vpd_sigma']:.2f} kPa the index reaches {cfg.th.moisture.target_w:.2f} in about {fc['hours_mid']:.0f} h (window {fc['hours_lo']:.0f} to {fc['hours_hi'] if fc['hours_hi'] is not None else 'open-ended'} h)." if fc["status"] == "ok" else "."))
    th_ = res.thermal
    if th_.get("status") == "ok":
        out["thermal"] = (f"Soil temperature follows air with time constant tau = {th_['tau_h']:.1f} h (fit R2 {th_['r2']}); a 24 h swing is damped to {th_['amplitude_ratio_24h'] * 100:.0f} % and delayed by {th_['phase_lag_h']:.1f} h. "
                          "Sun on the pot or a probe near a heater violates the model's assumption.")
    out["events"] = f"A watering is a rise of the index by >= {cfg.th.events.rise_w} within {cfg.th.events.rise_window_h:g} h seen by at least {cfg.th.events.min_probe_fraction * 100:.0f} % of valid probes, then {cfg.th.events.refractory_h:g} h refractory."
    return out
