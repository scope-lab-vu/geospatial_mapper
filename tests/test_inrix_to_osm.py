import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
from conftest import CONFLATION, write_conflation, write_geojson, xd_rows
from shapely.affinity import translate

import geo_mapper_api.converters as gma
import geo_mapper_api.inrix_to_osm as i2o


@pytest.fixture
def threads(monkeypatch):
    """Run parallel() on threads so the patched graph reaches its workers."""
    monkeypatch.setattr(i2o.concurrent.futures, "ProcessPoolExecutor", ThreadPoolExecutor)


def _edges(df, xd=None):
    rows = df if xd is None else df[df.XDSegID == xd]
    return sorted(zip(rows.u, rows.v, rows.key))


def test_xd_5_gets_its_own_blocks_and_not_the_next_one(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON")

    assert list(out.columns) == i2o.RESULT_COLUMNS
    xd5 = out[out.XDSegID == 5]
    assert _edges(xd5) == [(1, 2, 0), (2, 3, 0)]  # not (3, 5, 0), the next block past the XD's end
    assert set(xd5.source) == {"conflation"}
    assert set(xd5.osmid) == {100}
    assert xd5.overlap_m.between(89, 91).all()  # each block is 0.001 degrees of longitude, about 89.9 m
    assert not out.duplicated(["u", "v", "key", "XDSegID"]).any()
    for column in ["osm_geom", "inrix_geom"]:
        for geom in xd5[column]:
            minx, miny, maxx, maxy = geom.bounds
            assert -86.79 <= minx and maxx <= -86.77 and 36.15 <= miny and maxy <= 36.17


def test_stale_way_id_is_recovered_by_geometry(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON")

    xd8 = out[out.XDSegID == 8]
    assert _edges(xd8) == [(5, 6, 0)]
    assert set(xd8.source) == {"geometry"}
    assert set(xd8.osmid) == {400}


def test_result_carries_the_edge_road_type(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON")

    xd8 = out[out.XDSegID == 8]
    assert list(xd8.highway) == ["primary;secondary"]  # edge 5-6 merges a primary and a secondary way
    assert set(out[out.XDSegID == 5].highway) == {"primary"}


def test_service_road_row_carries_service(patched_graph):
    # The graph is patched, so this covers the conflation path and the column. The download filter is
    # covered by test_filters.py and the integration test.
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "LAKE")

    xd10 = out[out.XDSegID == 10]
    assert _edges(xd10) == [(13, 14, 0)]
    assert list(xd10.source) == ["conflation"]
    assert list(xd10.highway) == ["service"]


def test_fallback_does_not_put_an_xd_on_a_service_road(patched_graph, caplog):
    with caplog.at_level(logging.INFO, logger="geo_mapper_api.matching"):
        out = i2o.serial(patched_graph.geojson, patched_graph.csv, "LAKE")

    assert _edges(out, 11) == []
    summaries = [r for r in caplog.records if hasattr(r, "fallback_rejections")]
    assert len(summaries) == 1
    # Edge 13-14 points north, against XD 11. Its twin 14-13 runs with XD 11 but is a service road.
    assert summaries[0].fallback_rejections == {"direction": 1, "road_type": 1}


def test_interstate_xd_is_refused_on_a_residential_street(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON")
    assert _edges(out, 9) == []


def test_same_xd_with_a_street_name_gets_the_street(patched_graph, tmp_path):
    # Proves XD 9 above was refused by the road-level rule, not by a broken fallback.
    geojson = write_geojson(tmp_path / "test_ave.geojson", xd_rows(xd9_name="TEST AVE"))
    out = i2o.serial(geojson, patched_graph.csv, "DAVIDSON")
    assert _edges(out, 9) == [(2, 4, 0)]


def test_direction_keeps_only_the_northbound_edge(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "CROCKETT")
    assert _edges(out) == [(11, 12, 0)]


def test_pn_disagreement_is_logged_and_geometry_decides(patched_graph, tmp_path, caplog):
    stale = [(5, "100;101", "N;N")] + CONFLATION[1:]  # P/N says the westbound edges
    csv = write_conflation(tmp_path / "stale_pn.csv", stale)

    with caplog.at_level(logging.INFO, logger="geo_mapper_api.matching"):
        out = i2o.serial(patched_graph.geojson, csv, "DAVIDSON")

    assert _edges(out, 5) == [(1, 2, 0), (2, 3, 0)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "P/N" in r.getMessage()]
    # Both directions disagree: the eastbound edges (geometry yes, P/N no) and their westbound twins (the reverse).
    assert len(warnings) == 1 and " 4 " in warnings[0].getMessage()
    summaries = [r for r in caplog.records if hasattr(r, "pn_disagreements")]
    assert sum(r.pn_disagreements for r in summaries) == 4


def test_config_is_honoured(patched_graph):
    from geo_mapper_api import MatchConfig

    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON",
                     config=MatchConfig(band_half_width_m=2.5))
    assert 5 not in set(out.XDSegID)  # the XD lies 3 m from its edges


def test_config_must_be_a_match_config(tiny_inputs, monkeypatch):
    def must_not_download(polygon, custom_filter):
        raise AssertionError("get_osm_graph must not be called for a bad config")

    monkeypatch.setattr(gma, "get_osm_graph", must_not_download)
    with pytest.raises(TypeError, match="config"):
        i2o.serial(tiny_inputs.geojson, tiny_inputs.csv, "DAVIDSON", config="x")


def test_no_matches_returns_empty_result_columns(patched_graph, threads, tmp_path, caplog):
    far = [dict(row, geometry=translate(row["geometry"], yoff=0.005)) for row in xd_rows()]  # about 550 m north
    geojson = write_geojson(tmp_path / "far.geojson", far)

    for run in (i2o.serial, i2o.parallel):
        with caplog.at_level(logging.WARNING):
            out = run(geojson, patched_graph.csv, "DAVIDSON")
        assert out.empty
        assert list(out.columns) == i2o.RESULT_COLUMNS
        assert out["highway"].dtype == object
    assert any("no edges" in r.getMessage() for r in caplog.records)


def test_serial_accepts_single_string_county(patched_graph):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, "davidson")

    assert _edges(out, 5) == [(1, 2, 0), (2, 3, 0)]
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


def test_worker_count_keeps_reserved_cores_free():
    assert i2o._worker_count(10, 2, cpu_count=8) == 6
    assert i2o._worker_count(3, 2, cpu_count=8) == 3
    assert i2o._worker_count(10, 2, cpu_count=2) == 1
    assert i2o._worker_count(10, 0, cpu_count=4) == 4


@pytest.mark.parametrize("county", ["DAVIDSON", "CROCKETT"])  # CROCKETT needs no fallback, DAVIDSON does
def test_output_column_types(patched_graph, county):
    out = i2o.serial(patched_graph.geojson, patched_graph.csv, county)
    assert not out.empty
    for column in ["u", "v", "key", "osmid", "XDSegID"]:
        assert out[column].dtype == "int64", (column, out[column].dtype)
    assert out["overlap_m"].dtype == "float64"
    assert out["highway"].dtype == object
    assert all(isinstance(value, str) for value in out["highway"])
