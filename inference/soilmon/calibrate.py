"""Turn two time windows of real data into the dry/wet anchors for config/calibration.yaml.

  python -m soilmon.calibrate --csv readings.csv --dry "2026-10-25 08:00,2026-10-25 10:00" --wet "2026-10-26 18:00,2026-10-26 20:00"

Windows are in the site's local time.  Dry = the soil as dry as you will ever let it (or probes in dry air);
wet = shortly after a thorough watering, once the pot has stopped dripping.  The output is a YAML snippet to paste
into calibration.yaml.  It reports the median over each window and warns if a probe was noisy or unplugged.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

from . import clean
from .config import load_config
from .fetch import FetchError, fetch_supabase, load_csv


def _window(spec: str, tz: str):
    a, b = [x.strip() for x in spec.split(",")]
    return tuple(pd.Timestamp(x).tz_localize(tz).tz_convert("UTC") for x in (a, b))


def anchors(raw: pd.DataFrame, cfg, dry: str, wet: str) -> dict:
    ds = clean.normalize(raw, cfg, pd.Timestamp.now(tz="UTC"))
    s = ds.samples
    out = {}
    for name, spec in (("dry", dry), ("wet", wet)):
        a, b = _window(spec, cfg.tz)
        w = s.loc[a:b]
        for p in clean.PROBES:
            ok = w[f"{p}_mv_clean"].dropna()
            out.setdefault(p, {})[name] = {
                "mv": None if ok.empty else int(round(float(ok.median()))),
                "n": int(len(ok)), "n_window": int(len(w)),
                "iqr": None if ok.empty else int(round(float(ok.quantile(0.75) - ok.quantile(0.25)))),
            }
    return out


def to_yaml(a: dict) -> str:
    lines = ["  probes:"]
    for p, v in a.items():
        d, w = v["dry"]["mv"], v["wet"]["mv"]
        lines.append(f"    {p}: {{name: \"Probe {p[1]}\", dry_mv: {d if d is not None else 'null'}, wet_mv: {w if w is not None else 'null'}}}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["supabase", "csv"], default="csv")
    ap.add_argument("--csv")
    ap.add_argument("--dry", required=True, help='"YYYY-MM-DD HH:MM,YYYY-MM-DD HH:MM" local time')
    ap.add_argument("--wet", required=True)
    ap.add_argument("--config-dir", default="config")
    a = ap.parse_args(argv)
    cfg = load_config(a.config_dir)
    if a.source == "csv":
        if not a.csv:
            print("ERROR: --csv is required", file=sys.stderr)
            return 2
        raw = load_csv(a.csv)
    else:
        try:
            raw, _ = fetch_supabase(os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SECRET_KEY", ""), "readings", cfg.cal.site.device_id)
        except FetchError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    res = anchors(raw, cfg, a.dry, a.wet)
    bad = False
    for p, v in res.items():
        for k in ("dry", "wet"):
            r = v[k]
            note = ""
            if r["mv"] is None:
                note, bad = "  <- NO VALID READINGS in this window", True
            elif r["n"] < 0.8 * max(1, r["n_window"]):
                note = f"  <- only {r['n']} of {r['n_window']} readings were valid"
            elif r["iqr"] is not None and r["iqr"] > 60:
                note = f"  <- unsteady (IQR {r['iqr']} mV): wait longer or check the wiring"
            print(f"# {p} {k}: median {r['mv']} mV from {r['n']} readings{note}")
        if v["dry"]["mv"] is not None and v["wet"]["mv"] is not None and v["dry"]["mv"] - v["wet"]["mv"] < 300:
            print(f"# {p}: dry and wet are only {v['dry']['mv'] - v['wet']['mv']} mV apart: the index will be coarse or the windows are wrong")
    print("\n# paste under moisture: in config/calibration.yaml, then set calibrated: true")
    print(to_yaml(res))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
