"""End to end: raw rows -> cleaned data -> every indicator -> flags.  No I/O here."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import clean, derive, events as ev, forecast as fc, indicators as ind, status as st


@dataclass
class Result:
    ds: clean.Dataset
    now_ts: pd.Timestamp
    events: list = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    manual: pd.DataFrame = None
    forecast: dict = field(default_factory=dict)
    anomaly: dict = field(default_factory=dict)
    overwet: dict = field(default_factory=dict)
    humid: dict = field(default_factory=dict)
    vpd: dict = field(default_factory=dict)
    daily: pd.DataFrame = None
    heat: dict = field(default_factory=dict)
    light: dict = field(default_factory=dict)
    thermal: dict = field(default_factory=dict)
    cond: dict = field(default_factory=dict)
    comfort: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    now: dict = field(default_factory=dict)
    cards: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    overall: dict = field(default_factory=dict)
    explain: dict = field(default_factory=dict)

    @property
    def grid(self):
        return self.ds.grid


def run_pipeline(raw: pd.DataFrame, cfg, now=None, manual: pd.DataFrame | None = None, extra: dict | None = None) -> Result:
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    ds = clean.normalize(raw, cfg, now)
    if extra and extra.get("rows_null_ts") is not None:
        ds.report["rows_no_ts"] = max(ds.report.get("rows_no_ts", 0), int(extra["rows_null_ts"]))
    derive.add_derived(ds, cfg)
    clean.to_grid(ds, cfg)
    g = ds.grid
    res = Result(ds=ds, now_ts=now)
    res.manual = manual if manual is not None else pd.DataFrame(columns=["ts", "type", "volume_ml", "note"])

    detected = ev.detect_events(g, cfg)
    span = (g.index.min(), g.index.max()) if len(g) else None
    res.events, res.validation = ev.merge_with_manual(detected, res.manual, cfg, span)
    res.forecast = fc.dry_down(g, res.events, cfg, g.index.max() if len(g) else now)
    res.anomaly = fc.drying_anomaly(g, res.events, cfg)
    res.overwet = ind.overwet(g, cfg)
    res.humid = ind.humid_air(g, cfg)
    res.vpd = ind.vpd_state(g, cfg)
    res.daily, res.heat = ind.daily_summary(g, res.events, cfg)
    res.light = ind.light_state(res.daily, g, cfg)
    res.thermal = ind.thermal_lag(g, cfg)
    res.cond = ind.condensation(g, cfg)
    res.comfort = ind.comfort(g, cfg)
    res.quality = ind.quality(ds, g, res.events, cfg)
    res.now = st.now_values(ds, now, cfg.th.general.stale_warn_h)
    res.cards = {"moisture": st.moisture_card(g, res.now, res.forecast, cfg)}
    res.flags = st.build_flags(res, cfg)
    res.overall = st.overall(res.flags)
    res.explain = st.explain(res, cfg) if not ds.samples.empty else {}
    return res
