import subprocess
import sys

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
import pytest
from conftest import OFFSET_DEG, write_conflation, write_geojson
from shapely.geometry import LineString, Point

import geo_mapper_api.converters as gma

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
EAST_LINE = LineString([(-86.7800, 36.1600), (-86.7790, 36.1600)])


def test_merge_keeps_osm_geometry_named_geometry(tiny_inputs):
    _, edges = ox.graph_to_gdfs(tiny_inputs.graph)
    inrix = gpd.read_file(tiny_inputs.geojson)
    conflation = gma.load_conflation(tiny_inputs.csv, inrix["XDSegID"])

    merged = gma.merge_and_filter_inrix_osm(edges, conflation, inrix)

    assert "geometry" in merged.columns
    assert "geometry_inrix" in merged.columns
    assert "geometry_osm" not in merged.columns


def test_filter_groups_single_row_groups_and_empty_input():
    df = pd.DataFrame({
        "osmid": [1, 2, 3, 3],
        "XDSegID": [5, 6, 7, 7],
        "match": [True, False, True, False],
    })

    kept = gma.filter_groups_by_match(df)

    assert isinstance(kept, pd.DataFrame)
    assert sorted(zip(kept.osmid, kept.XDSegID, kept.match)) == [(1, 5, True), (2, 6, False), (3, 7, True)]
    assert gma.filter_groups_by_match(df.iloc[0:0]).empty


def test_bearing_table_matches_old_substring_rule():
    # The old code accepted an edge when the INRIX bearing was a substring of the OSM compass value.
    for inrix in ["N", "E", "S", "W"]:
        for compass in COMPASS:
            assert (compass in gma.ALLOWED_COMPASS[inrix]) == (inrix in compass), (inrix, compass)


def test_bearing_outside_table_keeps_all_candidates():
    df = pd.DataFrame({"XDSegID": [1, 2, 3, 4], "geometry": [EAST_LINE] * 4, "Bearing": ["O", "C", np.nan, "W"]})

    out = gma.apply_bearing_classification(df)

    assert list(out["compass_direction"]) == ["E"] * 4
    assert list(out["match"]) == [True, True, True, False]


def test_bearing_classification_empty_input():
    df = pd.DataFrame({
        "XDSegID": pd.Series([], dtype="int64"),
        "geometry": pd.Series([], dtype=object),
        "Bearing": pd.Series([], dtype=object),
    })

    out = gma.apply_bearing_classification(df)

    assert out.empty
    assert {"osm_line_bearing", "compass_direction", "match"} <= set(out.columns)


def test_conflation_single_ids_read_as_strings(tmp_path):
    path = write_conflation(tmp_path / "c.csv", [(5, "100", "P"), (6, "300", "N")])

    out = gma.load_conflation(path, [5, 6])

    assert out["OSMWayIDs"].dtype == "int64"
    assert sorted(out["OSMWayIDs"]) == [100, 300]


def test_conflation_length_mismatch_names_xd(tmp_path):
    path = write_conflation(tmp_path / "c.csv", [(5, "100;101", "P")])

    with pytest.raises(ValueError, match=r"differ in length for XDSegID \[5\]"):
        gma.load_conflation(path, [5])


def test_conflation_missing_direction_is_mismatch(tmp_path):
    path = write_conflation(tmp_path / "c.csv", [(5, "100", None)])

    with pytest.raises(ValueError, match=r"differ in length for XDSegID \[5\]"):
        gma.load_conflation(path, [5])


def test_conflation_missing_columns_names_file(tmp_path):
    path = tmp_path / "c.csv"
    pd.DataFrame({"XDSegID": [5], "OSMWayIDs": ["100"]}).to_csv(path, index=False)

    with pytest.raises(ValueError) as err:
        gma.load_conflation(path, [5])

    assert str(path) in str(err.value)
    assert "OSMWayDirections" in str(err.value)


def test_ids_non_numeric_name_file_and_values(tmp_path):
    trailing = write_conflation(tmp_path / "trailing.csv", [(5, "100;", "P;P")])
    with pytest.raises(ValueError) as err:
        gma.load_conflation(trailing, [5])
    assert str(trailing) in str(err.value) and "OSMWayIDs" in str(err.value)

    letters = write_conflation(tmp_path / "letters.csv", [(5, "abc", "P")])
    with pytest.raises(ValueError) as err:
        gma.load_conflation(letters, [5])
    assert str(letters) in str(err.value) and "OSMWayIDs" in str(err.value) and "abc" in str(err.value)

    fraction = write_conflation(tmp_path / "fraction.csv", [(5, "100.5", "P")])
    with pytest.raises(ValueError) as err:
        gma.load_conflation(fraction, [5])
    assert str(fraction) in str(err.value) and "OSMWayIDs" in str(err.value) and "100.5" in str(err.value)

    geojson = write_geojson(tmp_path / "bad.geojson", [
        {"County": "DAVIDSON", "XDSegID": "x1", "Bearing": "E", "geometry": EAST_LINE},
    ])
    with pytest.raises(ValueError) as err:
        gma.load_and_prepare_inrix_data_for_multiple_counties(geojson, ["DAVIDSON"])
    assert str(geojson) in str(err.value) and "XDSegID" in str(err.value) and "x1" in str(err.value)


def test_loader_unknown_county_lists_names(tiny_inputs):
    with pytest.raises(ValueError, match="NOWHERE"):
        gma.load_and_prepare_inrix_data_for_multiple_counties(tiny_inputs.geojson, ["DAVIDSON", "NOWHERE"])


def test_loader_missing_columns_names_file(tmp_path):
    geojson = write_geojson(tmp_path / "nobearing.geojson", [
        {"County": "DAVIDSON", "XDSegID": 5, "geometry": EAST_LINE},
    ])

    with pytest.raises(ValueError) as err:
        gma.load_and_prepare_inrix_data_for_multiple_counties(geojson, ["DAVIDSON"])

    assert str(geojson) in str(err.value)
    assert "Bearing" in str(err.value)


def test_loader_rejects_non_4326_geojson(tmp_path):
    path = tmp_path / "mercator.geojson"
    gpd.GeoDataFrame(
        {"County": ["DAVIDSON"], "XDSegID": [5], "Bearing": ["E"]}, geometry=[EAST_LINE], crs=4326,
    ).to_crs(3857).to_file(path, driver="GeoJSON")

    with pytest.raises(ValueError) as err:
        gma.load_and_prepare_inrix_data_for_multiple_counties(path, ["DAVIDSON"])

    assert str(path) in str(err.value) and "EPSG:4326" in str(err.value)


def test_normalize_counties_lowercase_and_duplicates():
    assert gma.normalize_counties(["davidson", " DAVIDSON ", "crockett"]) == ["DAVIDSON", "CROCKETT"]
    assert gma.normalize_counties("davidson") == ["DAVIDSON"]


def test_normalize_counties_rejects_empty():
    for bad in ([], [" "]):
        with pytest.raises(ValueError):
            gma.normalize_counties(bad)


def test_normalize_counties_rejects_non_str():
    for bad in ([None], [5]):
        with pytest.raises(ValueError):
            gma.normalize_counties(bad)


def test_exterior_polygon_of_single_line_is_buffered_polygon():
    line = LineString([(-86.7800, 36.1600), (-86.7700, 36.1600)])
    gdf = gpd.GeoDataFrame(geometry=[line], crs=4326)

    polygon = gma.get_exterior_polygon(gdf)

    assert polygon.geom_type == "Polygon"
    meters_per_degree_lat = 111_320
    assert polygon.contains(Point(-86.7750, 36.1600 + 150 / meters_per_degree_lat))
    assert not polygon.contains(Point(-86.7750, 36.1600 + 250 / meters_per_degree_lat))


def test_import_does_not_silence_warnings():
    # numpy adds "ignore" filters with message patterns on import; only a bare filterwarnings("ignore") counts.
    code = (
        "import warnings, geo_mapper_api.converters; "
        "assert not any(f[0] == 'ignore' and f[1] is None and f[2] is Warning for f in warnings.filters)"
    )
    result = subprocess.run([sys.executable, "-I", "-W", "default", "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _long_edge_group(xd_west, xd_east):
    """One osmnx edge (21 -> 22) about 600 m long, with an XD 5 m north of part of it."""
    north = 5 / 111_320
    group = pd.DataFrame({
        "u": [21], "v": [22], "key": [0], "osmid": [500], "XDSegID": [7],
        "geometry": [LineString([(-86.7900, 36.1500), (-86.7833, 36.1500)])],
        "geometry_inrix": [LineString([(xd_west, 36.1500 + north), (xd_east, 36.1500 + north)])],
        "osm_line_bearing": [90.0], "Bearing": ["E"], "compass_direction": ["E"],
    })
    nodes = gpd.GeoDataFrame(
        geometry=[Point(-86.7900, 36.1500), Point(-86.7833, 36.1500)],
        index=pd.Index([21, 22], name="osmid"),
    )
    return group, nodes


def _spy_on_fallback(monkeypatch):
    calls = []
    real = gma._fallback_overlap

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(gma, "_fallback_overlap", spy)
    return calls


def test_fallback_when_no_node_is_near(monkeypatch):
    calls = _spy_on_fallback(monkeypatch)
    group, nodes = _long_edge_group(-86.7878, -86.7856)

    out = gma.process_top_groups(group.groupby(["u", "v", "key"]), nodes)

    assert calls == [1]
    assert list(zip(out.u, out.v, out.key, out.XDSegID)) == [(21, 22, 0, 7)]


def test_fallback_when_only_one_node_is_near(monkeypatch):
    calls = _spy_on_fallback(monkeypatch)
    group, nodes = _long_edge_group(-86.7899, -86.7880)

    out = gma.process_top_groups(group.groupby(["u", "v", "key"]), nodes)

    assert calls == [1]
    assert list(zip(out.u, out.v, out.key, out.XDSegID)) == [(21, 22, 0, 7)]


def _pair_frame(rows):
    return pd.DataFrame(rows, columns=["u", "v", "key", "osmid", "XDSegID", "geometry", "geometry_inrix"])


def test_proximity_distance_is_in_meters():
    north = LineString([(x, y + OFFSET_DEG) for x, y in EAST_LINE.coords])
    df = _pair_frame([(1, 2, 0, 100, 5, EAST_LINE, north)])

    out = gma.check_osm_inrix_proximity(df, 25)

    assert out["distance"].iloc[0] == pytest.approx(3.0, abs=0.1)
    assert bool(out["within_threshold"].iloc[0])


def test_proximity_empty_input():
    out = gma.check_osm_inrix_proximity(_pair_frame([]), 25)

    assert out.empty
    assert list(out.columns) == [
        "u", "v", "key", "osmid", "XDSegID", "osm_geom", "inrix_geom", "distance", "within_threshold",
    ]
