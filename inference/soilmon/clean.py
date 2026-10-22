"""Row-level cleaning: types, timestamps, duplicates, per-channel validity, spike filter, gaps, grid."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# bit mask written by the firmware
FLAG_M_BAD = (1, 2, 4)
FLAG_DS_BAD, FLAG_DHT_BAD, FLAG_BH_BAD, FLAG_BH_SAT = 8, 16, 32, 64
FLAG_TS_UNSYNCED, FLAG_LOW_BATT, FLAG_DHT_ONE = 128, 256, 512

NUM_COLS = ["boot_id", "seq", "m1_mv", "m2_mv", "m3_mv", "m1_mad", "m2_mad", "m3_mad",
            "soil_t_c", "air_t_c", "rh_pct", "lux", "vbat_mv", "rssi", "flags"]
ALL_COLS = ["device_id", "ts", "created_at", "fw"] + NUM_COLS
PROBES = ("m1", "m2", "m3")


@dataclass
class Dataset:
    samples: pd.DataFrame                 # one row per upload, UTC DatetimeIndex, cleaned + derived columns
    grid: pd.DataFrame = None             # regular grid (median per bin), derived columns
    gaps: list = field(default_factory=list)       # [(start, end)] UTC Timestamps, gaps longer than 2.5 cadences
    report: dict = field(default_factory=dict)     # counts for the health panel
    cadence_s: float = 900.0


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ALL_COLS:
        if c not in df.columns:
            df[c] = np.nan if c in NUM_COLS else None
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    return df


def hampel_mask(x: pd.Series, window: int, sigmas: float, floor: float) -> pd.Series:
    """True where a value is a spike: |x - rolling median| > sigmas * 1.4826 * max(MAD, floor)."""
    med = x.rolling(window, center=True, min_periods=3).median()
    mad = (x - med).abs().rolling(window, center=True, min_periods=3).median()
    scale = 1.4826 * np.maximum(mad, floor)
    return ((x - med).abs() > sigmas * scale).fillna(False)


def _longest_equal_run(x: pd.Series) -> int:
    v = x.dropna()
    if v.empty:
        return 0
    runs = (v != v.shift()).cumsum()
    return int(v.groupby(runs).size().max())


def normalize(raw: pd.DataFrame, cfg, now: pd.Timestamp | None = None) -> Dataset:
    """Raw Supabase rows -> cleaned per-sample frame plus a report of everything that was discarded."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
    th, cal = cfg.th, cfg.cal
    rep: dict = {"rows_in": int(len(raw))}
    df = _coerce(raw)

    dev = cal.site.device_id
    if dev and "device_id" in df and df["device_id"].notna().any():
        rep["rows_other_device"] = int((df["device_id"] != dev).sum())
        df = df[df["device_id"] == dev]

    rep["rows_no_ts"] = int(df["ts"].isna().sum())
    lo = pd.Timestamp(th.general.ts_min_epoch, unit="s", tz="UTC")
    bad = df["ts"].notna() & ((df["ts"] < lo) | (df["ts"] > now + pd.Timedelta(days=1)))
    rep["rows_bad_ts"] = int(bad.sum())
    df = df[df["ts"].notna() & ~bad]

    n0 = len(df)
    df = df.drop_duplicates(subset=["device_id", "boot_id", "seq"], keep="first")
    rep["rows_duplicate"] = int(n0 - len(df))
    df = df.sort_values(["ts", "boot_id", "seq"]).set_index("ts")
    df.index.name = "ts"
    rep["rows_used"] = int(len(df))

    mcfg = cal.moisture
    fl = df["flags"].fillna(0).astype(int)
    # ---- moisture probes ------------------------------------------------------------
    for i, p in enumerate(PROBES):
        mv, mad = df[f"{p}_mv"], df[f"{p}_mad"]
        state = pd.Series("ok", index=df.index, dtype=object)
        state[mad > mcfg.mad_max_mv] = "noisy"
        state[(fl & FLAG_M_BAD[i]) > 0] = "flagged"
        state[mv >= mcfg.mv_max_valid] = "open"
        state[mv < mcfg.mv_min_valid] = "floating"
        state[mv.isna()] = "null"
        df[f"{p}_state"] = state
        df[f"{p}_ok"] = state == "ok"
        df[f"{p}_mv_clean"] = mv.where(df[f"{p}_ok"])

    # ---- temperatures, humidity, light -----------------------------------------------
    g = th.general
    soil = df["soil_t_c"].where(((fl & FLAG_DS_BAD) == 0) & df["soil_t_c"].between(-5, 60))
    air = df["air_t_c"].where(((fl & FLAG_DHT_BAD) == 0) & df["air_t_c"].between(-20, 60))
    rh = df["rh_pct"].where(((fl & FLAG_DHT_BAD) == 0) & df["rh_pct"].between(0, 100))
    spike_t = hampel_mask(air, g.hampel_window, g.hampel_sigmas, 0.3)
    spike_s = hampel_mask(soil, g.hampel_window, g.hampel_sigmas, 0.3)
    spike_h = hampel_mask(rh, g.hampel_window, g.hampel_sigmas, 1.5)
    rep["spikes_removed"] = int(spike_t.sum() + spike_s.sum() + spike_h.sum())
    df["soil_t_clean"] = soil.mask(spike_s)
    df["air_t_clean"] = air.mask(spike_t)
    df["rh_clean"] = rh.mask(spike_h)
    # a spike in either DHT22 channel makes the pair untrustworthy
    both = spike_t | spike_h
    df.loc[both, ["air_t_clean", "rh_clean"]] = np.nan
    df["lux_sat"] = (fl & FLAG_BH_SAT) > 0
    df["lux_clean"] = df["lux"].where(((fl & FLAG_BH_BAD) == 0) & (df["lux"] >= 0))

    # ---- gaps and cadence ------------------------------------------------------------
    dt = df.index.to_series().diff().dt.total_seconds().dropna()
    recent = dt[dt.index > df.index.max() - pd.Timedelta(days=14)] if len(dt) else dt
    cad = float(recent[(recent > 20) & (recent < 3 * cal.site.cycle_s)].median()) if len(recent) else np.nan
    cadence = cad if np.isfinite(cad) and cad > 0 else float(cal.site.cycle_s)
    gaps = []
    if len(dt):
        big = dt[dt > 2.5 * max(cadence, cal.site.cycle_s)]
        for t_end, d in big.items():
            gaps.append((t_end - pd.Timedelta(seconds=float(d)), t_end))
    rep["cadence_s"] = cadence
    rep["gaps"] = len(gaps)

    ds = Dataset(samples=df, gaps=gaps, report=rep, cadence_s=cadence)
    _link_quality(ds, now, th)
    return ds


def _link_quality(ds: Dataset, now: pd.Timestamp, th) -> None:
    """Sequence-gap loss, restarts, stuck sensors, per-channel valid fractions (last 24 h / 7 d)."""
    df, rep = ds.samples, ds.report
    if df.empty:
        return
    w7 = df[df.index >= df.index.max() - pd.Timedelta(days=7)]
    lost = 0
    for _, g in w7.groupby("boot_id"):
        s = g["seq"].dropna().astype(int)
        if len(s):
            lost += int(s.max() - s.min() + 1 - s.nunique())
    rep["seq_lost_7d"] = lost
    rep["seq_expected_7d"] = int(len(w7) + lost)
    rep["restarts_7d"] = int(max(0, w7["boot_id"].nunique() - 1))
    rep["boot_ids_total"] = int(df["boot_id"].nunique())
    w24 = df[df.index >= df.index.max() - pd.Timedelta(days=1)]
    rep["valid_frac_24h"] = {
        **{p: float(w24[f"{p}_ok"].mean()) for p in PROBES},
        "soil_t": float(w24["soil_t_clean"].notna().mean()),
        "air_t": float(w24["air_t_clean"].notna().mean()),
        "rh": float(w24["rh_clean"].notna().mean()),
        "lux": float(w24["lux_clean"].notna().mean()),
    }
    rep["probe_states_24h"] = {p: w24[f"{p}_state"].value_counts().to_dict() for p in PROBES}
    run = min(_longest_equal_run(w7["air_t_c"]), _longest_equal_run(w7["rh_pct"]))
    rep["dht_stuck_run"] = int(run)
    rep["dht_stuck"] = bool(run >= th.general.stuck_samples)
    rep["last_ts"] = df.index.max().isoformat()
    rep["first_ts"] = df.index.min().isoformat()
    rep["age_h"] = float((now - df.index.max()).total_seconds() / 3600.0)


def to_grid(ds: Dataset, cfg) -> pd.DataFrame:
    """Median per grid bin, full regular index, short gaps (<= max_interp_h) bridged for derived columns only."""
    df = ds.samples
    if df.empty:
        ds.grid = pd.DataFrame()
        return ds.grid
    step = f"{int(cfg.th.general.grid_min)}min"
    num = df.select_dtypes("number").drop(columns=["boot_id", "seq"], errors="ignore")
    num = num.assign(lux_sat=df["lux_sat"].astype(float), n=1.0)
    agg = {c: "median" for c in num.columns}
    agg["n"] = "sum"
    g = num.resample(step, label="left", closed="left").agg(agg)
    idx = pd.date_range(g.index.min(), g.index.max(), freq=step, tz="UTC")
    g = g.reindex(idx)
    g["n"] = g["n"].fillna(0)
    lim = max(1, int(round(cfg.th.general.max_interp_h * 60 / cfg.th.general.grid_min)))
    for c in g.columns:
        if c.endswith("_clean") or c in ("w_med", "w_spread", "vpd", "dew", "margin", "ppfd") or c in [f"w_{p}" for p in PROBES]:
            g[c] = g[c].interpolate(limit=lim, limit_area="inside")
    g.index.name = "ts"
    ds.grid = g
    return g
