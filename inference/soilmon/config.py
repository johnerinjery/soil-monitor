"""Load the two YAML files into attribute-style objects (cfg.cal.moisture.vcc_mv, cfg.th.vpd.low ...)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml


def _box(x):
    if isinstance(x, dict):
        return SimpleNamespace(**{k: _box(v) for k, v in x.items()})
    return x


def unbox(x):
    if isinstance(x, SimpleNamespace):
        return {k: unbox(v) for k, v in vars(x).items()}
    return x


class Config(SimpleNamespace):
    """cfg.cal (calibration.yaml), cfg.th (thresholds.yaml), cfg.root (repo root)."""

    @property
    def tz(self) -> str:
        return self.cal.site.timezone


def load_config(config_dir: str | Path = "config") -> Config:
    d = Path(config_dir)
    with open(d / "calibration.yaml", encoding="utf-8") as f:
        cal = yaml.safe_load(f)
    with open(d / "thresholds.yaml", encoding="utf-8") as f:
        th = yaml.safe_load(f)
    return Config(cal=_box(cal), th=_box(th), dir=d)


def default_config() -> Config:
    """Config from the repo's own config/ directory (used by tests and the simulator)."""
    here = Path(__file__).resolve().parents[2] / "config"
    return load_config(here)
