"""Command line entry point.

  python -m soilmon.run --source supabase --out web/data          (GitHub Actions; needs SUPABASE_URL + SUPABASE_SECRET_KEY)
  python -m soilmon.run --source csv --csv readings.csv --out web/data     (a CSV export of the table)
  python -m soilmon.run --source sim --days 30 --out web/data     (synthetic demo data, clearly labelled as such)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from .config import load_config
from .events import load_manual_log
from .export import write_all
from .fetch import FetchError, fetch_supabase, load_csv
from .pipeline import run_pipeline


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["supabase", "csv", "sim"], default="supabase")
    ap.add_argument("--csv", help="CSV file for --source csv")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--out", default="web/data")
    ap.add_argument("--days", type=float, default=30.0, help="length of the simulation")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--now", help="override 'now' (ISO, UTC); default is the wall clock")
    ap.add_argument("--table", default=os.environ.get("SUPABASE_TABLE", "readings"))
    a = ap.parse_args(argv)

    cfg = load_config(a.config_dir)
    now = pd.Timestamp(a.now, tz="UTC") if a.now else pd.Timestamp.now(tz="UTC")
    extra = None
    if a.source == "supabase":
        since = now - pd.Timedelta(days=cfg.th.export.fetch_days)
        try:
            raw, extra = fetch_supabase(os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SECRET_KEY", ""),
                                        a.table, cfg.cal.site.device_id, since)
        except FetchError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        label = "supabase"
    elif a.source == "csv":
        if not a.csv:
            print("ERROR: --csv is required with --source csv", file=sys.stderr)
            return 2
        raw, label = load_csv(a.csv), "csv"
    else:
        from .simulator import simulate
        start = now - pd.Timedelta(days=a.days)
        raw, _ = simulate(cfg, days=a.days, seed=a.seed, start=start.floor("15min"))
        label = "simulated"

    manual = load_manual_log(Path(a.config_dir).parent / cfg.cal.manual_log if not Path(cfg.cal.manual_log).exists() else cfg.cal.manual_log, cfg.tz)
    res = run_pipeline(raw, cfg, now=now, manual=manual, extra=extra)
    sizes = write_all(res, cfg, a.out, label, generated_at=now)
    rep = res.ds.report
    print(f"source={label} rows_in={rep['rows_in']} used={rep.get('rows_used', 0)} no_ts={rep.get('rows_no_ts', 0)} "
          f"dups={rep.get('rows_duplicate', 0)} events={len(res.events)} status={res.overall['level']} flags={len(res.flags)}")
    print("wrote " + ", ".join(f"{k} ({v / 1024:.0f} kB)" for k, v in sizes.items()) + f" -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
