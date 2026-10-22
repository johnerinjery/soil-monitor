"""Row-level derived quantities: moisture index per probe, median and spread, VPD, dew point, PPFD."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import physics as ph
from .clean import PROBES, Dataset


def anchors_g25(cfg) -> dict:
    """Conductance anchors (dry, wet) per probe at the calibration temperature, from the mV anchors."""
    m = cfg.cal.moisture
    out = {}
    for p in PROBES:
        pc = getattr(m.probes, p)
        gd = float(ph.conductance_s(pc.dry_mv, m.vcc_mv, m.r_fixed_ohm))
        gw = float(ph.conductance_s(pc.wet_mv, m.vcc_mv, m.r_fixed_ohm))
        out[p] = (gd, gw)
    return out


def add_derived(ds: Dataset, cfg) -> Dataset:
    df, m, a = ds.samples, cfg.cal.moisture, cfg.cal.air
    anc = anchors_g25(cfg)

    # temperature used for conductance compensation: soil, else air, else the calibration temperature
    t_comp = df["soil_t_clean"].fillna(df["air_t_clean"]).fillna(m.cal_temp_c)
    df["t_comp"] = t_comp
    for p in PROBES:
        g = ph.conductance_s(df[f"{p}_mv_clean"].to_numpy(float), m.vcc_mv, m.r_fixed_ohm)
        g25 = ph.temp_compensate(g, t_comp.to_numpy(float), m.tc_per_degC, m.cal_temp_c)
        gd, gw = anc[p]
        df[f"w_{p}"] = ph.index_from_g25(g25, gd, gw, m.mapping)
    w = df[[f"w_{p}" for p in PROBES]]
    n = w.notna().sum(axis=1)
    df["w_med"] = w.median(axis=1).where(n >= cfg.th.moisture.min_probes_for_median)
    df["w_spread"] = (w.max(axis=1) - w.min(axis=1)).where(n >= 2)
    df["w_n"] = n

    t, rh = df["air_t_clean"], df["rh_clean"]
    df["vpd"] = ph.vpd_kpa(t.to_numpy(float), rh.to_numpy(float))
    df["vpd_sigma"] = ph.vpd_sigma_kpa(t.to_numpy(float), rh.to_numpy(float), a.dht_sigma_t_c, a.dht_sigma_rh_pct)
    df["dew"] = ph.dew_point_c(t.to_numpy(float), rh.to_numpy(float))
    df["margin"] = df["soil_t_clean"] - df["dew"]
    df["ppfd"] = ph.ppfd_from_lux(df["lux_clean"].to_numpy(float), cfg.cal.light.ppfd_per_lux)
    ds.samples = df
    return ds
