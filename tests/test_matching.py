"""Matching rules on plain lines in meters (no CRS, no network)."""
import math

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString

HALF_WIDTH = 15.0
XD = LineString([(0, 0), (100, 0)])


@pytest.fixture
def matching():
    from geo_mapper_api import matching
    return matching


@pytest.fixture
def config():
    from geo_mapper_api.config import MatchConfig
    return MatchConfig()


def arc(radius, start_deg, end_deg, steps=90, center=(0, 0)):
    """A polyline along a circle, counter-clockwise when end_deg > start_deg."""
    angles = np.linspace(math.radians(start_deg), math.radians(end_deg), steps + 1)
    return LineString([(center[0] + radius * math.cos(a), center[1] + radius * math.sin(a)) for a in angles])


def ring(radius, start_deg, clockwise=False, steps=120):
    """An exactly closed circle starting and ending at start_deg."""
    end = start_deg - 360 if clockwise else start_deg + 360
    line = arc(radius, start_deg, end, steps)
    coords = list(line.coords)
    coords[-1] = coords[0]
    return LineString(coords)


def piece(matching, edge, xd=XD):
    return matching.band_piece(edge, xd, HALF_WIDTH)


def test_collinear_edge_counts_its_length(matching):
    along, intervals, runs_with = piece(matching, LineString([(10, 1), (60, 1)]))
    assert along == pytest.approx(50)
    assert runs_with is True
    assert intervals == [pytest.approx((10, 60))]


def test_perpendicular_crossing_counts_almost_nothing(matching):
    along, _, _ = piece(matching, LineString([(50, -30), (50, 30)]))
    assert along < 0.01


def test_side_streets_at_the_xd_end_count_almost_nothing(matching):
    straight_out = LineString([(100, 0), (100, 40)])
    angle = math.radians(89)
    angled_out = LineString([(100, 0), (100 + 40 * math.cos(angle), 40 * math.sin(angle))])
    assert piece(matching, straight_out)[0] < 0.01
    assert piece(matching, angled_out)[0] < 1


def test_edge_continuing_past_the_end_counts_almost_nothing(matching):
    along, intervals, runs_with = piece(matching, LineString([(100, 0), (150, 0)]))
    assert along < 0.01
    assert runs_with is False and intervals == []


def test_edge_split_70_30_is_kept_by_both_xds(matching, config):
    edge = LineString([(30, 1), (130, 1)])
    second_xd = LineString([(100, 0), (200, 0)])
    along_first = piece(matching, edge)[0]
    along_second = piece(matching, edge, second_xd)[0]
    assert along_first == pytest.approx(70) and along_second == pytest.approx(30)
    assert along_first >= matching.required_overlap_m(100, config.min_overlap_m)
    assert along_second >= matching.required_overlap_m(100, config.min_overlap_m)


def test_ramp_curving_onto_the_xd_runs_with_it(matching):
    ramp = LineString([(-50, 40), (-20, 30), (0, 14), (10, 2), (60, 1)])
    along, _, runs_with = piece(matching, ramp)
    assert runs_with is True
    assert along > 50


def test_loop_edge_on_a_roundabout_arc_both_directions(matching):
    xd = arc(20, -90, 0)  # the last quarter before the loop node at (20, 0), counter-clockwise
    assert piece(matching, ring(20, 0), xd)[2] is True
    assert piece(matching, ring(20, 0, clockwise=True), xd)[2] is False


@pytest.mark.parametrize("start", [0, 90, 135, 180])
def test_closed_ring_xd_direction_at_any_start(matching, start):
    xd = ring(20, 0)
    assert piece(matching, ring(20, start), xd)[2] is True
    assert piece(matching, ring(20, start, clockwise=True), xd)[2] is False


def test_edge_across_a_closed_ring_xd_seam_counts_its_own_extent(matching):
    xd = ring(20, 0)
    half_angle = math.degrees(5 / 20)  # a 10 m arc centred on the start and end node
    along, _, runs_with = piece(matching, arc(20, -half_angle, half_angle, steps=10), xd)
    assert along == pytest.approx(10, abs=0.5)
    assert runs_with is True


def test_edge_bridging_a_near_closed_ring_gap_excludes_the_gap(matching):
    gap_angle = math.degrees(7.5 / 20)
    xd = arc(20, gap_angle / 2, 360 - gap_angle / 2, steps=180)  # its end is about 7.5 m from its start
    bridge = arc(20, -math.degrees(10 / 20), math.degrees(10 / 20), steps=20)  # 20 m across the gap
    along, _, _ = piece(matching, bridge, xd)
    assert 10 < along < 15


def test_edge_inside_a_curve_counts_the_xd_stretch_beside_it(matching):
    along, _, runs_with = piece(matching, arc(8, 0, 90), arc(20, 0, 90))
    assert along == pytest.approx(math.pi * 20 / 2, abs=0.5)
    assert runs_with is True


def test_edge_leaving_and_reentering_the_band_skips_the_gap(matching):
    detour = LineString([(10, 1), (30, 1), (40, 30), (60, 30), (70, 1), (90, 1)])
    along, intervals, _ = piece(matching, detour)
    assert 40 < along < 60
    assert len(intervals) == 2


def test_point_touch_and_outside_edges_have_no_extent(matching):
    for edge in (LineString([(50, 15), (50, 40)]), LineString([(0, 50), (100, 50)])):
        assert piece(matching, edge) == (0.0, [], False)


def test_diverging_45_degree_edge_is_the_known_limit(matching):
    leg = 30 / math.sqrt(2)
    along, _, _ = piece(matching, LineString([(80, 0), (80 + leg, leg)]))
    assert along >= 10


def test_edge_drawn_opposite_to_the_xd_does_not_run_with_it(matching):
    assert piece(matching, LineString([(60, 1), (10, 1)]))[2] is False


def test_required_overlap(matching):
    assert matching.required_overlap_m(8, 10) == 4
    assert matching.required_overlap_m(100, 10) == 10


@pytest.mark.parametrize("road_name, frc, highway, allowed", [
    ("I-99 E", 2, "motorway", True),
    ("I-99 E", 2, "residential", False),
    ("I-99 E", 1, ["motorway_link", "trunk"], True),
    ("US-999", 2, "primary", True),
    ("US-999", 2, "motorway", True),
    ("BETA ROAD", 4, "motorway", False),
    ("BETA ROAD", 4, "motorway_link", True),
    ("BETA ROAD", 4, ["trunk", "motorway"], False),
    (None, 4, "motorway", False),
    (float("nan"), 2, "motorway", True),
    (float("nan"), 1, "residential", True),
])
def test_road_level_rule(matching, road_name, frc, highway, allowed):
    assert matching.road_level_allows(road_name, frc, highway) is allowed


@pytest.mark.parametrize("highway, allowed", [
    ("service", False),
    (["service", "residential"], True),
    ("residential;service", True),
    ("busway", False),
    ("rest_area", False),
    ("escape", False),
    (["busway", "service"], False),
    ("residential", True),
    ("motorway_link", True),
    ("road", True),
    ("living_street", True),
    (float("nan"), False),
])
def test_fallback_type_rule(matching, highway, allowed):
    assert matching.fallback_type_allows(highway) is allowed


@pytest.mark.parametrize("highway, label", [
    ("primary", "primary"),
    (["service", "residential"], "residential;service"),
    (["primary", "primary"], "primary"),
    ("service;residential", "residential;service"),
])
def test_highway_label(matching, highway, label):
    assert matching.highway_label(highway) == label


@pytest.mark.parametrize("highway", [float("nan"), None, "", []])
def test_highway_label_refuses_an_edge_without_a_road_type(matching, highway):
    with pytest.raises(ValueError, match="no OSM highway tag"):
        matching.highway_label(highway)


@pytest.mark.parametrize("oneway, reversed_, two_way", [
    (False, False, True),
    (np.bool_(False), np.bool_(True), True),
    (True, False, False),
    ([False, True], False, False),
    (False, [False, True], False),
])
def test_is_two_way(matching, oneway, reversed_, two_way):
    assert matching.is_two_way(oneway, reversed_) is two_way


@pytest.mark.parametrize("direction, reversed_, runs_with", [
    ("P", False, True),
    ("P", True, False),
    ("N", True, True),
    ("N", np.bool_(False), False),
])
def test_pn_runs_with(matching, direction, reversed_, runs_with):
    assert matching.pn_runs_with(direction, reversed_) is runs_with


@pytest.mark.parametrize("length, intervals, share", [
    (100, [(0, 100)], 1.0),
    (100, [], 0.0),
    (40, [(15, 25)], 0.25),
    (100, [(0, 35), (65, 100)], 0.7),
    (100, [(0, 60), (40, 100)], 1.0),
])
def test_xd_coverage(matching, length, intervals, share):
    assert matching.xd_coverage(length, intervals) == pytest.approx(share)


def fallback_inputs(road_name, frc, edge, highway):
    edges = gpd.GeoDataFrame({"u": [1], "v": [2], "key": [0], "osmid": [[500, 501]], "highway": [highway]},
                             geometry=[edge])
    xds = gpd.GeoDataFrame({"XDSegID": [7], "FRC": [frc], "RoadName": [road_name]}, geometry=[XD])
    return edges, xds


def test_fallback_refuses_a_crossing_edge(matching, config):
    edges, xds = fallback_inputs("TEST ST", 4, LineString([(50, -30), (50, 30)]), "residential")
    rows, rejected = matching.geometry_fallback(edges, xds, set(), config)
    assert rows.empty
    assert rejected == {"overlap": 1}


def test_fallback_refuses_an_interstate_xd_on_a_residential_edge(matching, config):
    edges, xds = fallback_inputs("I-1 N", 1, LineString([(10, 1), (90, 1)]), "residential")
    rows, rejected = matching.geometry_fallback(edges, xds, set(), config)
    assert rows.empty
    assert rejected == {"road_level": 1}


def test_fallback_refuses_a_service_edge(matching, config):
    edges, xds = fallback_inputs("DELTA ROAD", 4, LineString([(10, 1), (90, 1)]), "service")
    rows, rejected = matching.geometry_fallback(edges, xds, set(), config)
    assert rows.empty
    assert rejected == {"road_type": 1}


def test_fallback_accepts_a_ramp_xd_on_a_motorway_link(matching, config):
    edges, xds = fallback_inputs("999X", 4, LineString([(10, 1), (90, 1)]), "motorway_link")
    rows, rejected = matching.geometry_fallback(edges, xds, set(), config)
    assert list(zip(rows.u, rows.v, rows.key, rows.XDSegID)) == [(1, 2, 0, 7)]
    assert list(rows.osmid) == [500]
    assert rows.overlap_m.iloc[0] == pytest.approx(80)
    assert rejected == {}


def test_fallback_skips_a_pair_the_way_id_path_saw(matching, config):
    edges, xds = fallback_inputs("999X", 4, LineString([(10, 1), (90, 1)]), "motorway_link")
    rows, _ = matching.geometry_fallback(edges, xds, {(1, 2, 0, 7)}, config)
    assert rows.empty
