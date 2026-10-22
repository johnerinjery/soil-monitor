import numpy as np
import pytest

from soilmon import physics as ph


def test_saturation_vapor_pressure_reference():
    assert ph.sat_vapor_pressure_kpa(25.0) == pytest.approx(3.169, abs=0.01)
    assert ph.sat_vapor_pressure_kpa(0.0) == pytest.approx(0.6108, abs=1e-4)


def test_vpd_reference_and_limits():
    assert ph.vpd_kpa(25.0, 50.0) == pytest.approx(1.585, abs=0.01)
    assert ph.vpd_kpa(25.0, 100.0) == pytest.approx(0.0, abs=1e-12)
    assert ph.vpd_kpa(25.0, 130.0) == 0.0            # RH is clipped, never a negative deficit


def test_dew_point_reference():
    assert ph.dew_point_c(25.0, 50.0) == pytest.approx(13.9, abs=0.2)
    assert ph.dew_point_c(20.0, 100.0) == pytest.approx(20.0, abs=1e-6)
    assert ph.dew_point_c(30.0, 40.0) < 30.0


def test_vpd_sigma_matches_finite_difference():
    t, rh, st, sr = 24.0, 55.0, 0.5, 3.0
    d_t = (ph.vpd_kpa(t + 1e-4, rh) - ph.vpd_kpa(t - 1e-4, rh)) / 2e-4
    d_rh = (ph.vpd_kpa(t, rh + 1e-3) - ph.vpd_kpa(t, rh - 1e-3)) / 2e-3
    assert ph.vpd_sigma_kpa(t, rh, st, sr) == pytest.approx(np.hypot(d_t * st, d_rh * sr), rel=1e-3)


def test_divider_roundtrip():
    g = np.array([1e-5, 5e-5, 2e-4])
    v = ph.mv_from_conductance(g, 3300, 10000)
    assert ph.conductance_s(v, 3300, 10000) == pytest.approx(g, rel=1e-9)


def test_conductance_edge_cases():
    assert ph.conductance_s(3300, 3300, 10000) == 0.0          # open circuit: no conductance
    assert ph.conductance_s(3400, 3300, 10000) == 0.0          # above Vcc never gives negative G
    assert np.isnan(ph.conductance_s(0, 3300, 10000))          # shorted: undefined, not infinite


def test_temperature_compensation():
    assert ph.temp_compensate(1.2, 35.0, 0.02, 25.0) == pytest.approx(1.0)
    assert ph.temp_compensate(1.0, 25.0) == pytest.approx(1.0)
    assert ph.temp_compensate(0.8, 15.0, 0.02, 25.0) == pytest.approx(1.0)


@pytest.mark.parametrize("mapping", ["linear", "log"])
def test_index_anchors_and_roundtrip(mapping):
    gd, gw = 6e-6, 2.3e-4
    assert ph.index_from_g25(gd, gd, gw, mapping) == pytest.approx(0.0, abs=1e-9)
    assert ph.index_from_g25(gw, gd, gw, mapping) == pytest.approx(1.0, abs=1e-9)
    assert ph.index_from_g25(gw * 5, gd, gw, mapping) == 1.0     # clipped
    assert ph.index_from_g25(0.0, gd, gw, mapping) == 0.0
    w = np.linspace(0, 1, 11)
    back = ph.index_from_g25(ph.g25_from_index(w, gd, gw, mapping), gd, gw, mapping)
    assert back == pytest.approx(w, abs=1e-9)


def test_log_mapping_is_monotone_and_concave_in_g():
    gd, gw = 6e-6, 2.3e-4
    mid = ph.index_from_g25((gd + gw) / 2, gd, gw, "log")
    assert 0.5 < mid < 1.0            # log scale gives dry soils more resolution


def test_ppfd_and_dli():
    assert ph.ppfd_from_lux(1000, 0.0185) == pytest.approx(18.5)
    assert ph.dli_mol(np.full(40, 100.0), np.full(40, 900.0)) == pytest.approx(3.6)      # 100 umol for 10 h
    assert ph.dli_mol(np.array([np.nan, 100.0]), np.array([900.0, 900.0])) == pytest.approx(0.09)


def test_thermal_response():
    a, lag = ph.thermal_response(0.0)
    assert a == pytest.approx(1.0) and lag == pytest.approx(0.0)
    a, lag = ph.thermal_response(1e6)
    assert a < 1e-3 and lag == pytest.approx(6.0, abs=0.01)         # quarter of the 24 h period
    a, lag = ph.thermal_response(4.0)
    assert a == pytest.approx(1 / np.sqrt(1 + (2 * np.pi / 24 * 4) ** 2))
