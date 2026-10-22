"""Synthetic node data with known ground truth, in the exact shape of the Supabase `readings` table.

The generator is the test bench for the whole pipeline: it injects the failure modes seen on the real node
(floating 142 mV channels, loose-wire noise, NULL timestamps and sensor cells, power loss with a new boot_id,
spikes, duplicates) and records the truth (watering times, drying coefficient, thermal time constant).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import physics as ph
from .clean import PROBES
from .derive import anchors_g25

ADC_CAP_MV = 3100.0


def _ar1(n, rng, phi, sd):
    x = np.zeros(n)
    e = rng.normal(0, sd * np.sqrt(1 - phi ** 2), n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def simulate(cfg, days: float = 21.0, seed: int = 1, start: str | pd.Timestamp = "2026-10-01T00:00:00Z",
             glitches: bool = True, k_true: float = 0.11, tau_true_h: float = 4.0,
             water_every_h: float = 132.0, first_water_h: float = 20.0, humid_spell: tuple | None = None,
             cycle_s: int = 900):
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else pd.Timestamp(start, tz="UTC")
    n = int(days * 86400 / cycle_s)
    idx = pd.date_range(t0, periods=n, freq=f"{cycle_s}s")
    hl = ((idx - t0).total_seconds() / 3600.0 + (t0.hour + t0.minute / 60 + 5.5)).to_numpy() % 24   # local hour (IST)
    dt_h = cycle_s / 3600.0

    ta = 24 + 3.5 * np.sin(2 * np.pi * (hl - 9) / 24) + _ar1(n, rng, 0.995, 1.2) + rng.normal(0, 0.08, n)
    rh = 58 - 3.0 * (ta - 24) + _ar1(n, rng, 0.995, 5.0) + rng.normal(0, 0.4, n)
    if humid_spell:
        a, b = (int(humid_spell[0] * 3600 / cycle_s), int((humid_spell[0] + humid_spell[1]) * 3600 / cycle_s))
        rh[a:b] = 88 + rng.normal(0, 1.0, b - a)
    rh = np.clip(rh, 25, 97)
    cloud = np.clip(0.75 + _ar1(n, rng, 0.999, 0.25), 0.25, 1.0)
    sun = 12000.0 * np.maximum(0.0, np.sin(np.pi * (hl - 6) / 12)) * cloud
    lamp = np.where((hl >= 18) & (hl < 22), 800.0, 0.0)
    lux = np.maximum(0.0, (sun + lamp) * (1 + rng.normal(0, 0.03, n)))
    ts_soil = np.empty(n)
    ts_soil[0] = ta[0]
    a_ = 1 - np.exp(-dt_h / tau_true_h)
    for i in range(1, n):
        ts_soil[i] = ts_soil[i - 1] + a_ * (ta[i - 1] - ts_soil[i - 1])
    ts_soil += rng.normal(0, 0.03, n)

    vpd = ph.vpd_kpa(ta, rh)
    water_at = []
    t = first_water_h
    while t < days * 24 - 6:
        water_at.append(t)
        t += water_every_h * (1 + rng.uniform(-0.08, 0.08))
    w = np.empty(n)
    w[0] = 0.5
    soak = {}
    for h in water_at:                           # a watering soaks in over two samples (~30 min)
        i = int(round(h * 3600 / cycle_s))
        soak[i] = 0.93 + 0.06 * rng.random()
    for i in range(1, n):
        if i in soak:
            w[i] = w[i - 1] + 0.6 * (soak[i] - w[i - 1])
        elif i - 1 in soak:
            w[i] = soak[i - 1]
        else:
            w[i] = max(0.02, w[i - 1] - k_true * vpd[i] * dt_h / 24.0)

    m, anc = cfg.cal.moisture, anchors_g25(cfg)
    gain, off = (1.0, 0.93, 1.05), (0.0, -0.03, 0.04)
    rows = {}
    for j, p in enumerate(PROBES):
        wp = np.clip(w * gain[j] + off[j] + rng.normal(0, 0.008, n), 0, 1)
        gd, gw = anc[p]
        g25 = ph.g25_from_index(wp, gd, gw, m.mapping)
        g = g25 * (1 + m.tc_per_degC * (ts_soil - m.cal_temp_c))
        mv = np.minimum(ph.mv_from_conductance(g, m.vcc_mv, m.r_fixed_ohm), ADC_CAP_MV) + rng.normal(0, 4, n)
        rows[f"{p}_mv"] = np.round(mv).astype(float)
        rows[f"{p}_mad"] = rng.integers(2, 7, n).astype(float)

    seq = np.arange(n, dtype=float)
    boot = np.full(n, float(rng.integers(1, 2 ** 31)))
    df = pd.DataFrame({
        "device_id": cfg.cal.site.device_id, "boot_id": boot, "seq": seq, "ts": idx,
        **rows, "soil_t_c": np.round(ts_soil, 2), "air_t_c": np.round(ta, 1), "rh_pct": np.round(rh, 1),
        "lux": np.round(lux, 1), "vbat_mv": np.nan, "rssi": rng.integers(-72, -58, n).astype(float),
        "flags": np.zeros(n), "fw": "1.0.1",
    })
    truth = {"events": [t0 + pd.Timedelta(hours=h) for h in water_at], "k": k_true, "tau_h": tau_true_h,
             "w": pd.Series(w, index=idx), "glitches": {}}

    if glitches:
        G = truth["glitches"]

        def rowsel(a_h, b_h):
            return (df.index >= int(a_h * 3600 / cycle_s)) & (df.index < int(b_h * 3600 / cycle_s))

        # 1. first boot: clock not yet synced
        df.loc[:2, "ts"] = pd.NaT
        df.loc[:2, "flags"] = 128
        # 2. probe 3 floating for ~1.5 days
        s = rowsel(days * 24 * 0.40, days * 24 * 0.40 + 36)
        df.loc[s, ["m3_mv", "m3_mad"]] = [142.0, 0.0]
        df.loc[s, "flags"] = df.loc[s, "flags"].astype(int) | 4
        G["floating_probe3"] = (idx[s.argmax()], idx[len(s) - 1 - s[::-1].argmax()])
        # 3. loose wire on probe 2
        s = rowsel(days * 24 * 0.72, days * 24 * 0.72 + 3)
        df.loc[s, "m2_mv"] = df.loc[s, "m2_mv"] + rng.normal(0, 300, s.sum())
        df.loc[s, "m2_mad"] = rng.integers(26, 48, s.sum()).astype(float)
        # 4. DHT22 failure and BH1750 failure
        s = rowsel(days * 24 * 0.25, days * 24 * 0.25 + 2)
        df.loc[s, ["air_t_c", "rh_pct"]] = np.nan
        df.loc[s, "flags"] = df.loc[s, "flags"].astype(int) | 16
        s = rowsel(days * 24 * 0.30, days * 24 * 0.30 + 1.25)
        df.loc[s, "lux"] = np.nan
        df.loc[s, "flags"] = df.loc[s, "flags"].astype(int) | 32
        # 5. isolated DHT22 spikes
        for frac in (0.18, 0.47, 0.83):
            i = int(n * frac)
            df.loc[i, "air_t_c"] += 15
            df.loc[i, "rh_pct"] = 100.0
        # 6. power loss: 10 h without records, then a new boot_id with seq restarting at 0
        a_i = int(n * 0.55)
        b_i = a_i + int(10 * 3600 / cycle_s)
        df = df.drop(index=range(a_i, b_i))
        after = df.index >= b_i
        df.loc[after, "boot_id"] = float(rng.integers(1, 2 ** 31))
        df.loc[after, "seq"] = df.loc[after, "seq"] - b_i
        G["power_loss"] = (idx[a_i], idx[b_i])
        # 7. duplicates (a resent batch) and shuffled arrival order
        df = pd.concat([df, df.sample(5, random_state=seed)], ignore_index=True)
        df = df.sample(frac=1.0, random_state=seed).reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
    df["created_at"] = df["ts"] + pd.Timedelta(seconds=20)
    cols = ["device_id", "boot_id", "seq", "ts", "m1_mv", "m2_mv", "m3_mv", "m1_mad", "m2_mad", "m3_mad",
            "soil_t_c", "air_t_c", "rh_pct", "lux", "vbat_mv", "rssi", "flags", "fw", "created_at"]
    return df[cols], truth
