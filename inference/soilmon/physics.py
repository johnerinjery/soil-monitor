"""Pure physics / unit conversions. No pandas state, no I/O: every function is testable on numbers or arrays."""
from __future__ import annotations

import numpy as np

# Magnus-Tetens constants (Alduchov & Eskridge / Tetens form, valid roughly -40..50 degC)
_A, _B, _C = 0.6108, 17.27, 237.3


def sat_vapor_pressure_kpa(t_c):
    """Saturation vapour pressure over water [kPa]: es = 0.6108 exp(17.27 T / (T + 237.3))."""
    t = np.asarray(t_c, dtype=float)
    return _A * np.exp(_B * t / (t + _C))


def vpd_kpa(t_c, rh_pct):
    """Vapour-pressure deficit [kPa] = es(T) (1 - RH/100), RH clipped to 0..100."""
    rh = np.clip(np.asarray(rh_pct, dtype=float), 0.0, 100.0)
    return sat_vapor_pressure_kpa(t_c) * (1.0 - rh / 100.0)


def vpd_sigma_kpa(t_c, rh_pct, sigma_t=0.5, sigma_rh=3.0):
    """First-order propagated 1-sigma uncertainty of VPD from DHT22 temperature and humidity errors."""
    t = np.asarray(t_c, dtype=float)
    rh = np.clip(np.asarray(rh_pct, dtype=float), 0.0, 100.0)
    es = sat_vapor_pressure_kpa(t)
    des_dt = es * _B * _C / (t + _C) ** 2
    d_t = des_dt * (1.0 - rh / 100.0) * sigma_t
    d_rh = es / 100.0 * sigma_rh
    return np.sqrt(d_t ** 2 + d_rh ** 2)


def dew_point_c(t_c, rh_pct):
    """Dew point [degC] from the Magnus formula (RH floored at 1 % to avoid log 0)."""
    t = np.asarray(t_c, dtype=float)
    rh = np.clip(np.asarray(rh_pct, dtype=float), 1.0, 100.0)
    g = np.log(rh / 100.0) + _B * t / (_C + t)
    return _C * g / (_B - g)


# ---------------------------------------------------------------- moisture chain
def conductance_s(v_mv, vcc_mv, r_fixed_ohm):
    """Probe conductance [S] from the divider output.

    The module is a divider  Vout = Vcc * Rp / (Rp + Rf)   (dry = high voltage, wet = low voltage),
    so  Rp = Rf V / (Vcc - V)  and  G = 1/Rp = (Vcc - V) / (Rf V).
    """
    v = np.asarray(v_mv, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        g = (vcc_mv - v) / (r_fixed_ohm * v)
    g = np.where(v <= 0, np.nan, g)
    return np.maximum(g, 0.0)


def temp_compensate(g, t_c, tc=0.02, t_ref=25.0):
    """Refer conductance to t_ref:  G25 = G / (1 + tc (T - t_ref))."""
    return np.asarray(g, dtype=float) / (1.0 + tc * (np.asarray(t_c, dtype=float) - t_ref))


def index_from_g25(g25, g_dry, g_wet, mapping="linear"):
    """Moisture index W in [0, 1] between the dry and wet conductance anchors."""
    g = np.asarray(g25, dtype=float)
    if mapping == "log":
        eps = max(g_dry, 1e-9) * 0.5 + 1e-9
        num = np.log(g + eps) - np.log(g_dry + eps)
        den = np.log(g_wet + eps) - np.log(g_dry + eps)
    else:
        num, den = g - g_dry, g_wet - g_dry
    if den == 0:
        return np.full_like(g, np.nan)
    return np.clip(num / den, 0.0, 1.0)


def g25_from_index(w, g_dry, g_wet, mapping="linear"):
    """Inverse of index_from_g25 (used by the simulator and the tests)."""
    w = np.asarray(w, dtype=float)
    if mapping == "log":
        eps = max(g_dry, 1e-9) * 0.5 + 1e-9
        return np.exp(np.log(g_dry + eps) + w * (np.log(g_wet + eps) - np.log(g_dry + eps))) - eps
    return g_dry + w * (g_wet - g_dry)


def mv_from_conductance(g, vcc_mv, r_fixed_ohm):
    """Forward divider model: V = Vcc / (1 + Rf G)."""
    return vcc_mv / (1.0 + r_fixed_ohm * np.asarray(g, dtype=float))


# ---------------------------------------------------------------- light
def ppfd_from_lux(lux, factor):
    """PPFD [umol m-2 s-1] = factor * lux (source dependent, see calibration.yaml)."""
    return np.asarray(lux, dtype=float) * factor


def dli_mol(ppfd_umol, dt_s):
    """Daily light integral contribution [mol m-2] = sum(PPFD * dt) * 1e-6."""
    return float(np.nansum(np.asarray(ppfd_umol, dtype=float) * np.asarray(dt_s, dtype=float)) * 1e-6)


# ---------------------------------------------------------------- thermal
def thermal_response(tau_h, period_h=24.0):
    """One-pole lag: amplitude ratio 1/sqrt(1+(w tau)^2) and phase lag [h] arctan(w tau)/w."""
    w = 2 * np.pi / period_h
    return 1.0 / np.sqrt(1.0 + (w * tau_h) ** 2), np.arctan(w * tau_h) / w
