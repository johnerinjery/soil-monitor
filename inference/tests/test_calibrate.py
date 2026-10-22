from pathlib import Path

import pandas as pd

from soilmon import calibrate
from soilmon.simulator import simulate

ROOT = Path(__file__).resolve().parents[2]


def test_anchors_from_windows(cfg0, tmp_path, capsys):
    raw, truth = simulate(cfg0, days=8, seed=9, glitches=False)
    csv = tmp_path / "r.csv"
    raw.to_csv(csv, index=False)
    t = truth["events"][0]
    wet = f"{(t + pd.Timedelta(hours=1)).tz_convert('Asia/Kolkata').strftime('%Y-%m-%d %H:%M')},{(t + pd.Timedelta(hours=3)).tz_convert('Asia/Kolkata').strftime('%Y-%m-%d %H:%M')}"
    dry_t = truth["events"][1] - pd.Timedelta(hours=8)
    dry = f"{dry_t.tz_convert('Asia/Kolkata').strftime('%Y-%m-%d %H:%M')},{(dry_t + pd.Timedelta(hours=2)).tz_convert('Asia/Kolkata').strftime('%Y-%m-%d %H:%M')}"
    rc = calibrate.main(["--csv", str(csv), "--dry", dry, "--wet", wet, "--config-dir", str(ROOT / "config")])
    out = capsys.readouterr().out
    assert rc == 0 and "dry_mv" in out and "wet_mv" in out
    a = calibrate.anchors(raw, cfg0, dry, wet)
    for p in ("m1", "m2", "m3"):
        assert a[p]["wet"]["mv"] < a[p]["dry"]["mv"]            # wetter soil reads lower
        assert a[p]["wet"]["n"] >= 6


def test_empty_window_is_reported(cfg0, tmp_path, capsys):
    raw, _ = simulate(cfg0, days=3, seed=9, glitches=False)
    csv = tmp_path / "r.csv"
    raw.to_csv(csv, index=False)
    rc = calibrate.main(["--csv", str(csv), "--dry", "2020-01-01 00:00,2020-01-01 01:00", "--wet", "2020-01-02 00:00,2020-01-02 01:00", "--config-dir", str(ROOT / "config")])
    assert rc == 1 and "NO VALID READINGS" in capsys.readouterr().out
