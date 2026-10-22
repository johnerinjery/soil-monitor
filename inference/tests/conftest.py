import copy
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "inference"))

from soilmon.config import load_config  # noqa: E402
from soilmon.simulator import simulate  # noqa: E402


@pytest.fixture(scope="session")
def cfg0():
    return load_config(ROOT / "config")


@pytest.fixture()
def cfg(cfg0):
    return copy.deepcopy(cfg0)


@pytest.fixture(scope="session")
def sim_dirty(cfg0):
    return simulate(cfg0, days=21, seed=1, glitches=True)


@pytest.fixture(scope="session")
def sim_clean(cfg0):
    return simulate(cfg0, days=21, seed=2, glitches=False)


GRID_COLS = ["w_m1", "w_m2", "w_m3", "w_med", "w_spread", "vpd", "vpd_sigma", "dew", "margin", "ppfd", "soil_t_clean",
             "air_t_clean", "rh_clean", "lux_clean", "lux_sat", "n"]


def blank_grid(start="2026-10-01T00:00:00Z", steps=96, step_min=15):
    idx = pd.date_range(pd.Timestamp(start), periods=steps, freq=f"{step_min}min", tz=None if pd.Timestamp(start).tzinfo else "UTC")
    g = pd.DataFrame(np.nan, index=idx, columns=GRID_COLS)
    g["lux_sat"] = 0.0
    g["n"] = 1.0
    g.index.name = "ts"
    return g


def set_w(g, w_all=None, **probes):
    """Set per-probe index series; w_med = median of the probes."""
    for p in ("m1", "m2", "m3"):
        g[f"w_{p}"] = probes.get(p, w_all)
    g["w_med"] = g[["w_m1", "w_m2", "w_m3"]].median(axis=1)
    g["w_spread"] = g[["w_m1", "w_m2", "w_m3"]].max(axis=1) - g[["w_m1", "w_m2", "w_m3"]].min(axis=1)
    return g
