from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
from conftest import write_conflation

import geo_mapper_api.converters as gma
import geo_mapper_api.inrix_to_osm as i2o


@pytest.fixture
def threads(monkeypatch):
    """Run parallel() on threads so the patched graph reaches its workers."""
    monkeypatch.setattr(i2o.concurrent.futures, "ProcessPoolExecutor", ThreadPoolExecutor)


def _edges(df):
    return sorted(zip(df.u, df.v, df.key))


def test_serial_end_to_end_returns_eastbound_main_st(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON")

    assert list(out.columns) == i2o.RESULT_COLUMNS
    assert _edges(out) == [(1, 2, 0), (2, 3, 0)]
    assert set(out.XDSegID) == {5}
    assert set(out.osmid) == {100}
    assert not out.duplicated(["u", "v", "key", "XDSegID"]).any()
    for column in ["osm_geom", "inrix_geom"]:
        for geom in out[column]:
            minx, miny, maxx, maxy = geom.bounds
            assert -86.79 <= minx and maxx <= -86.77 and 36.15 <= miny and maxy <= 36.17


def test_threshold_is_honoured_in_meters(patched_graph):
    inside = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON", threshold_distance=3.5)
    outside = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON", threshold_distance=2.5)

    assert _edges(inside) == [(1, 2, 0), (2, 3, 0)]
    assert outside.empty
    assert list(outside.columns) == i2o.RESULT_COLUMNS


def test_no_matches_returns_empty_result_columns(patched_graph, threads, tmp_path):
    csv = write_conflation(tmp_path / "nomatch.csv", [(5, "999", "P"), (6, "300", "P")])

    for run in (i2o.serial, i2o.parallel):
        out = run(patched_graph.geojson, csv, "DAVIDSON")
        assert out.empty
        assert list(out.columns) == i2o.RESULT_COLUMNS


def test_serial_accepts_single_string_county(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "davidson")

    assert _edges(out) == [(1, 2, 0), (2, 3, 0)]
    assert set(out.county) == {"DAVIDSON"}


def test_empty_county_list_raises(patched_graph, threads):
    for run in (i2o.serial, i2o.parallel):
        with pytest.raises(ValueError):
            run(patched_graph.geojson, patched_graph.csv, [])


def test_bad_conflation_raises_value_error_before_county_work(tiny_inputs, monkeypatch, tmp_path):
    def must_not_download(polygon, custom_filter):
        raise AssertionError("get_osm_graph must not be called for bad input")

    monkeypatch.setattr(gma, "get_osm_graph", must_not_download)
    csv = write_conflation(tmp_path / "bad.csv", [(5, "100;101", "P"), (6, "300", "P")])

    with pytest.raises(ValueError, match="differ in length"):
        i2o.serial(tiny_inputs.geojson, csv, "DAVIDSON")


def test_parallel_equals_serial(patched_graph, threads):
    counties = ["DAVIDSON", "CROCKETT"]

    serial = i2o.serial(patched_graph.geojson, patched_graph.csv, counties)
    parallel = i2o.parallel(patched_graph.geojson, patched_graph.csv, counties)

    pd.testing.assert_frame_equal(parallel, serial)
    assert list(serial.county.unique()) == ["CROCKETT", "DAVIDSON"]


def test_failure_names_county(tiny_inputs, monkeypatch, threads):
    def overpass_down(polygon, custom_filter):
        raise ConnectionError("overpass down")

    monkeypatch.setattr(gma, "get_osm_graph", overpass_down)

    for run in (i2o.serial, i2o.parallel):
        with pytest.raises(RuntimeError) as err:
            run(tiny_inputs.geojson, tiny_inputs.csv, "DAVIDSON")
        assert str(err.value) == "county 'DAVIDSON' failed: ConnectionError: overpass down"


def test_worker_count_keeps_two_cores_free():
    assert i2o._worker_count(10, cpu_count=8) == 6
    assert i2o._worker_count(3, cpu_count=8) == 3
    assert i2o._worker_count(10, cpu_count=2) == 1
    assert i2o._worker_count(10, cpu_count=1) == 1


def test_bearing_O_xd_kept(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "CROCKETT")

    assert _edges(out) == [(11, 12, 0), (12, 11, 0)]
    assert set(out.XDSegID) == {6}
