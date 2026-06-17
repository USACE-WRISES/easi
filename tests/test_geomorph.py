"""Offline tests for the cross-section geomorphology math (ported from xs-calc)."""
from __future__ import annotations

import pytest

from easi import geomorph


def test_top_width_simple_trapezoid():
    # flat valley at 10, V-notch channel 40..60 with thalweg 6 at 50
    stations = list(range(0, 101, 2))
    elevs = []
    for x in stations:
        if 40 <= x <= 60:
            elevs.append(6 + abs(x - 50) * (4 / 10))  # 6 at 50 rising to 10 at 40/60
        else:
            elevs.append(10.0)
    # at stage 10 the whole valley is wet -> width = 100
    T, merged = geomorph.top_width(stations, elevs, 10.0)
    assert T == pytest.approx(100.0, abs=0.01)
    # at stage 8 only the channel core is wet (elev<=8 between ~45 and ~55)
    T2, _ = geomorph.top_width(stations, elevs, 8.0)
    assert 8 <= T2 <= 12


def test_top_width_merges_islands():
    # two separate pools split by a mid bar above stage
    stations = [0, 1, 2, 3, 4, 5, 6]
    elevs = [0, 0, 5, 0, 0, 5, 0]  # bars at x=2 and x=5
    T, merged = geomorph.top_width(stations, elevs, 1.0)
    assert len(merged) >= 2  # distinct wetted spans (islands)
    assert T > 0


def test_entrenchment_connected_vs_incised():
    stations = list(range(0, 101))
    # connected: low banks (floodplain at 10), channel depth 4 (thalweg 6)
    connected = [10.0 if not (40 <= x <= 60) else 6 + abs(x - 50) * 0.4 for x in stations]
    # incised: high banks (terrace at 14), same channel
    incised = [14.0 if not (40 <= x <= 60) else 6 + abs(x - 50) * 0.8 for x in stations]
    d_bf, w_bf = 2.0, 10.0
    er_c = geomorph.transect_entrenchment(connected, [connected[i] for i in range(len(connected))], d_bf, w_bf)
    # (use elevs lists directly)
    er_conn = geomorph.transect_entrenchment(stations, connected, d_bf, w_bf)
    er_inc = geomorph.transect_entrenchment(stations, incised, d_bf, w_bf)
    assert er_conn and er_inc
    # connected spreads wide at flood-prone stage -> high ER; incised stays narrow
    assert er_conn["er"] > er_inc["er"]
    assert er_conn["er"] >= 2.0          # not entrenched
    assert er_inc["er"] < 2.0            # entrenched


def test_bankfull_geometry_monotonic():
    w_small, d_small = geomorph.bankfull_geometry(5)
    w_big, d_big = geomorph.bankfull_geometry(4000)
    assert w_big > w_small > 0 and d_big > d_small > 0


def test_bank_height_ratio_connected_low():
    stations = list(range(0, 101))
    connected = [10.0 if not (45 <= x <= 55) else 8 + abs(x - 50) * 0.4 for x in stations]
    bhr = geomorph.bank_height_ratio(stations, connected, d_bf=2.0)
    assert bhr is not None and bhr <= 1.3   # bank ~2 m above thalweg / 2 m bankfull


def test_top_of_bank_elev_picks_lower_bank():
    stations = list(range(0, 101))
    # left bank crest 12, right bank crest 15, thalweg 6 at x=50
    elevs = []
    for x in stations:
        if x < 50:
            elevs.append(12.0 if x <= 40 else 12 - (x - 40) * 0.6)
        else:
            elevs.append(15.0 if x >= 60 else 6 + (x - 50) * 0.9)
    tob = geomorph.top_of_bank_elev(stations, elevs)
    assert tob == pytest.approx(12.0, abs=0.5)   # the lower of the two banks


def test_derive_from_stages_bhr_and_depth():
    stations = list(range(0, 101))
    connected = [10.0 if not (40 <= x <= 60) else 6 + abs(x - 50) * 0.4 for x in stations]
    # bankfull at 8 (2 m above thalweg 6); floodplain = bankfull -> BHR 1.0 (connected)
    d = geomorph.derive_from_stages(stations, connected, thalweg=6.0,
                                    bankfull_stage=8.0, floodplain_stage=8.0)
    assert d["bankfull_depth_max_m"] == pytest.approx(2.0, abs=1e-6)
    assert d["bank_height_ratio"] == pytest.approx(1.0, abs=1e-6)
    assert d["entrenchment_ratio"] is not None and d["entrenchment_ratio"] > 1.0
    # incised: terrace/floodplain at 12 -> BHR = (12-6)/2 = 3.0
    d2 = geomorph.derive_from_stages(stations, connected, thalweg=6.0,
                                     bankfull_stage=8.0, floodplain_stage=12.0)
    assert d2["bank_height_ratio"] == pytest.approx(3.0, abs=1e-6)


def test_derive_from_stages_er_rises_with_flood_prone_width():
    stations = list(range(0, 101))
    connected = [10.0 if not (40 <= x <= 60) else 6 + abs(x - 50) * 0.4 for x in stations]
    incised = [14.0 if not (40 <= x <= 60) else 6 + abs(x - 50) * 0.8 for x in stations]
    er_c = geomorph.derive_from_stages(stations, connected, thalweg=6.0,
                                       bankfull_stage=8.0, floodplain_stage=8.0)["entrenchment_ratio"]
    er_i = geomorph.derive_from_stages(stations, incised, thalweg=6.0,
                                       bankfull_stage=8.0, floodplain_stage=8.0)["entrenchment_ratio"]
    assert er_c > er_i           # connected spreads wide at the flood-prone stage


def test_derive_from_stages_invalid_bankfull_returns_none():
    stations, elevs = list(range(0, 11)), [5.0] * 11
    d = geomorph.derive_from_stages(stations, elevs, thalweg=5.0,
                                    bankfull_stage=5.0, floodplain_stage=6.0)
    assert d["entrenchment_ratio"] is None and d["bank_height_ratio"] is None


def test_balanced_profile_recenters_offset_thalweg():
    # transect sampled symmetric about the flowline vertex (0), but the channel low
    # point sits at station -50 -> a thalweg-datumed plot would be lopsided.
    stations = list(range(-200, 201, 25))                  # 17 pts, symmetric about 0
    elevs = [5.0 + abs(s - (-50)) * 0.1 for s in stations]  # V min (thalweg) at -50
    out = geomorph.balanced_profile(stations, elevs)
    assert out is not None
    out_s, out_e = out
    assert len(out_s) >= 7
    assert out_s == sorted(out_s) and len(set(out_s)) == len(out_s)   # strictly increasing
    assert out_s[0] == pytest.approx(-out_s[-1])                      # symmetric about the thalweg
    assert out_s[out_e.index(min(out_e))] == pytest.approx(0.0)       # thalweg recentred to 0


def test_balanced_profile_extends_short_side_to_taller_bank():
    # thalweg at 0; left tops out low+near (h=2 by -40, then flat); right rises to a tall
    # crest far out (h=8 by +200). The short (left) side must be shown out to the right's
    # crest distance, not cut at its own low bank.
    stations = list(range(-300, 301, 20))
    elevs = [min(2.0, abs(s) * 0.05) if s <= 0 else min(8.0, s * 0.04) for s in stations]
    out = geomorph.balanced_profile(stations, elevs)
    assert out is not None
    out_s, out_e = out
    assert out_s[out_e.index(min(out_e))] == pytest.approx(0.0)  # thalweg at center
    assert out_s[0] == pytest.approx(-out_s[-1])                # symmetric
    assert abs(out_s[-1]) == pytest.approx(200.0)              # reach = right's crest distance
    assert abs(out_s[0]) > 100                                 # left extended past its own low bank (~40)


def test_balanced_profile_trims_terrace_on_incised_reach():
    # banks crest at ~+/-30 (h=5) then a flat terrace out to +/-300 -> stay tight, not +/-300.
    stations = list(range(-300, 301, 5))
    elevs = [min(5.0, abs(s) * (5.0 / 30.0)) for s in stations]
    out = geomorph.balanced_profile(stations, elevs)
    assert out is not None
    out_s, _ = out
    assert out_s[0] == pytest.approx(-out_s[-1])
    assert abs(out_s[-1]) <= 50                                # trimmed near the bank crest (~30)


def test_balanced_profile_guards():
    assert geomorph.balanced_profile([0, 1, 2], [0, 1, 2]) is None       # < 7 points
    assert geomorph.balanced_profile([0, 1, 2, 3, 4, 5, 6], [0] * 5) is None  # length mismatch
    # thalweg at the very edge -> symmetric data limit below min_half -> None
    stations = list(range(-200, 201, 10))
    elevs = [5.0 + abs(s - (-190)) * 0.1 for s in stations]
    assert geomorph.balanced_profile(stations, elevs) is None


def test_reach_summary_injected_bankfull_overrides_national():
    stations = list(range(0, 101))
    elevs = [10.0 if not (40 <= x <= 60) else 6 + abs(x - 50) * 0.4 for x in stations]
    rs = geomorph.reach_summary([(stations, elevs)], 50.0,
                                bankfull=(12.0, 1.5), division="Interior Plains")
    assert rs["bankfull_width_m"] == 12.0 and rs["bankfull_depth_m"] == 1.5
    assert rs["bankfull_division"] == "Interior Plains"


def test_reach_summary_retains_profile_and_stages():
    stations = list(range(0, 101))
    elevs = [10.0 if not (40 <= x <= 60) else 6 + abs(x - 50) * 0.4 for x in stations]
    rs = geomorph.reach_summary([(stations, elevs)], 50.0)
    # new representative-profile keys for the hydraulics + cross-section plot
    assert rs["profile"]["stations"] and rs["profile"]["elevs"]
    assert rs["thalweg"] == pytest.approx(6.0, abs=1e-6)
    assert rs["top_of_bank_m"] == pytest.approx(10.0, abs=0.5)
    assert "fp_stage_m" in rs
    # existing aggregates unchanged
    assert "entrenchment_ratio" in rs and "bank_height_ratio" in rs
