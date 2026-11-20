import subprocess
import sys

import geopandas as gpd
import pandas as pd
import pytest
from conftest import write_conflation, write_geojson
from shapely.geometry import LineString, Point

import geo_mapper_api.converters as gma

EAST_LINE = LineString([(-86.7800, 36.1600), (-86.7790, 36.1600)])


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

    for value in ("100.5", "inf"):
        path = write_conflation(tmp_path / "numbers.csv", [(5, value, "P")])
        with pytest.raises(ValueError) as err:
            gma.load_conflation(path, [5])
        assert str(path) in str(err.value) and "OSMWayIDs" in str(err.value) and value in str(err.value)

    geojson = write_geojson(tmp_path / "bad.geojson", [
        {"County": "DAVIDSON", "XDSegID": "x1", "FRC": 4, "RoadName": "MAIN ST", "geometry": EAST_LINE},
    ])
    with pytest.raises(ValueError) as err:
        gma.load_and_prepare_inrix_data_for_multiple_counties(geojson, ["DAVIDSON"])
    assert str(geojson) in str(err.value) and "XDSegID" in str(err.value) and "x1" in str(err.value)


def test_loader_unknown_county_lists_names(tiny_inputs):
    with pytest.raises(ValueError, match="NOWHERE"):
        gma.load_and_prepare_inrix_data_for_multiple_counties(tiny_inputs.geojson, ["DAVIDSON", "NOWHERE"])


def test_loader_missing_columns_names_file(tmp_path):
    geojson = write_geojson(tmp_path / "nofrc.geojson", [
        {"County": "DAVIDSON", "XDSegID": 5, "RoadName": "MAIN ST", "geometry": EAST_LINE},
    ])

    with pytest.raises(ValueError) as err:
        gma.load_and_prepare_inrix_data_for_multiple_counties(geojson, ["DAVIDSON"])

    assert str(geojson) in str(err.value)
    assert "FRC" in str(err.value)


@pytest.mark.parametrize("frc, shown", [(6, "6"), (0, "0"), ("x", "x")])
def test_loader_rejects_bad_frc(tmp_path, frc, shown):
    geojson = write_geojson(tmp_path / "frc.geojson", [
        {"County": "DAVIDSON", "XDSegID": 5, "FRC": frc, "RoadName": "MAIN ST", "geometry": EAST_LINE},
    ])

    with pytest.raises(ValueError) as err:
        gma.load_and_prepare_inrix_data_for_multiple_counties(geojson, ["DAVIDSON"])

    assert str(geojson) in str(err.value) and "FRC" in str(err.value) and shown in str(err.value)


@pytest.mark.parametrize("geometry", [None, LineString([(-86.78, 36.16), (-86.78, 36.16)])])
def test_loader_rejects_empty_or_zero_length_lines(tmp_path, geometry):
    path = tmp_path / "degenerate.geojson"
    gpd.GeoDataFrame(
        {"County": ["DAVIDSON", "DAVIDSON"], "XDSegID": [5, 77], "FRC": [4, 4], "RoadName": ["A", "B"]},
        geometry=[EAST_LINE, geometry], crs=4326,
    ).to_file(path, driver="GeoJSON")

    with pytest.raises(ValueError) as err:
        gma.load_and_prepare_inrix_data_for_multiple_counties(path, ["DAVIDSON"])

    assert str(path) in str(err.value) and "77" in str(err.value)


def test_loader_rejects_non_4326_geojson(tmp_path):
    path = tmp_path / "mercator.geojson"
    gpd.GeoDataFrame(
        {"County": ["DAVIDSON"], "XDSegID": [5], "FRC": [4], "RoadName": ["MAIN ST"]}, geometry=[EAST_LINE], crs=4326,
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

    polygon = gma.get_exterior_polygon(gdf, 200)

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
