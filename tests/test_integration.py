"""End-to-end run on real downtown Nashville data (licensed, never committed).

Build the data with scripts/make_test_data.py, then run:
    GEO_MAPPER_TEST_DATA=tests/data pytest tests/test_integration.py
The first run downloads the OSM network into <data>/osm_cache; later runs reuse it.
"""
import logging
import os
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import pandas as pd
import pytest

import geo_mapper_api.inrix_to_osm as i2o

DATA = Path(os.environ.get("GEO_MAPPER_TEST_DATA", ""))
GEOJSON = DATA / "downtown.geojson"
CSV = DATA / "downtown.csv"

pytestmark = pytest.mark.skipif(
    not os.environ.get("GEO_MAPPER_TEST_DATA") or not GEOJSON.is_file() or not CSV.is_file(),
    reason="set GEO_MAPPER_TEST_DATA to a folder with downtown.geojson and downtown.csv",
)

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def result():
    # osmnx internals run here; the strict chained-assignment setting in conftest is for this package's code.
    with pytest.MonkeyPatch.context() as mp, pd.option_context("mode.chained_assignment", "warn"):
        mp.setattr(ox.settings, "use_cache", True)
        mp.setattr(ox.settings, "cache_folder", str(DATA / "osm_cache"))
        yield i2o.serial(GEOJSON, CSV, "DAVIDSON", 25)


@pytest.fixture(scope="module")
def segments():
    return gpd.read_file(GEOJSON)


def test_output_not_empty(result, segments):
    assert not result.empty
    coverage = result["XDSegID"].nunique() / segments["XDSegID"].nunique()
    logger.info("coverage: %d of %d downtown XDs have at least one edge (%.1f%%)",
                result["XDSegID"].nunique(), segments["XDSegID"].nunique(), 100 * coverage)


def test_every_pair_is_in_the_conflation(result):
    conflation = pd.read_csv(CSV, dtype={"OSMWayIDs": str})
    allowed = {(int(way), int(xd))
               for xd, ways in zip(conflation["XDSegID"], conflation["OSMWayIDs"])
               if isinstance(ways, str)
               for way in ways.split(";")}
    pairs = set(zip(result["osmid"].astype(int), result["XDSegID"].astype(int)))
    assert pairs <= allowed, sorted(pairs - allowed)[:5]


def test_distances_and_area(result, segments):
    assert (result["distance"] <= 25).all()
    utm = segments.estimate_utm_crs()
    area = gpd.GeoSeries([segments.to_crs(utm).unary_union.convex_hull.buffer(500)], crs=utm).to_crs(4326).iloc[0]
    outside = [g for g in result["osm_geom"] if not area.contains(g)]
    assert not outside, outside[:3]


def test_no_duplicate_pairs(result):
    assert not result.duplicated(["u", "v", "key", "XDSegID"]).any()
