"""End-to-end runs on real INRIX data (licensed, never committed).

Build the data with scripts/make_test_data.py, then run:
    GEO_MAPPER_TEST_DATA=tests/data pytest tests/test_integration.py
The first run downloads each area's OSM network into <data>/osm_cache; later runs reuse it.
tests/data/answer_key.csv, once reviewed, adds hand-checked expectations for downtown XDs.
"""
import logging
import os
from pathlib import Path

import osmnx as ox
import pandas as pd
import pytest

import geo_mapper_api.converters as gma
import geo_mapper_api.inrix_to_osm as i2o
from geo_mapper_api import MatchConfig, evaluation, matching
from geo_mapper_api.filters import EXCLUDED_HIGHWAY

DATA = Path(os.environ.get("GEO_MAPPER_TEST_DATA", ""))
AREAS = {"downtown": "DAVIDSON", "crockett": "CROCKETT"}
ANSWER_KEY = DATA / "answer_key.csv"
CONFIG = MatchConfig()

logger = logging.getLogger(__name__)


def _area_files(area):
    return DATA / f"{area}.geojson", DATA / f"{area}.csv"


def _available(area):
    return bool(os.environ.get("GEO_MAPPER_TEST_DATA")) and all(p.is_file() for p in _area_files(area))


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture(scope="module", params=list(AREAS))
def run(request):
    area = request.param
    if not _available(area):
        pytest.skip(f"set GEO_MAPPER_TEST_DATA to a folder with {area}.geojson and {area}.csv")
    geojson, csv = _area_files(area)
    collect = _Collect()
    matching_logger = logging.getLogger("geo_mapper_api.matching")
    matching_logger.addHandler(collect)
    old_level = matching_logger.level
    matching_logger.setLevel(logging.INFO)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ox.settings, "use_cache", True)
            mp.setattr(ox.settings, "cache_folder", str(DATA / "osm_cache"))
            result = i2o.serial(geojson, csv, AREAS[area], config=CONFIG)  # serial: logs stay in this process
            xds = gma.load_and_prepare_inrix_data_for_multiple_counties(geojson, [AREAS[area]])
            edges = evaluation.area_edges(xds, CONFIG)  # inside the patch, so it reads the test cache
            summary = evaluation.summary(result, xds, edges, CONFIG)
    finally:
        matching_logger.removeHandler(collect)
        matching_logger.setLevel(old_level)
    summary["P/N disagreements"] = sum(getattr(r, "pn_disagreements", 0) for r in collect.records)
    logger.info("%s summary: %s", area, summary)
    return {"area": area, "result": result, "xds": xds, "csv": csv, "summary": summary, "edges": edges}


def test_graph_has_only_road_types(run):
    # The anchored filter removes exact values; a raw multi-value such as "footway;steps" passes by design.
    excluded = set(EXCLUDED_HIGHWAY)
    for value in run["edges"]["highway"]:
        ways = value if isinstance(value, list) else [value]
        assert ways and all(isinstance(w, str) and w not in excluded for w in ways), value
    if run["area"] == "downtown":
        assert any("service" in matching.road_types(v) for v in run["edges"]["highway"])


def test_coverage(run):
    # Judged over matched XDs that have an OSM road nearby; the rest are OSM data gaps, reported apart.
    s = run["summary"]
    assert s["of those, >= 90% covered"] >= 0.9 * s["matched XDs with a road nearby"], s


def test_unmatched_share(run):
    s = run["summary"]
    assert s["unmatched XDs"] <= 0.05 * len(run["xds"]), s


def test_no_wrong_level_matches_at_interstate_crossings(run):
    s = run["summary"]
    assert s["wrong-level matches"] == 0 and s["wrong-level rows unresolved"] == 0, s


def test_no_touch_only_edges(run):
    # A regression guard: the runs-along rule is what excludes these.
    assert run["summary"]["touch-only edges"] == 0


def test_conflation_pairs_are_in_the_conflation(run):
    conflation = pd.read_csv(run["csv"], dtype={"OSMWayIDs": str})
    allowed = {(int(way), int(xd))
               for xd, ways in zip(conflation["XDSegID"], conflation["OSMWayIDs"])
               if isinstance(ways, str)
               for way in ways.split(";")}
    rows = run["result"][run["result"]["source"] == "conflation"]
    pairs = set(zip(rows["osmid"].astype(int), rows["XDSegID"].astype(int)))
    assert pairs <= allowed, sorted(pairs - allowed)[:5]


def test_no_duplicate_pairs(run):
    assert not run["result"].duplicated(["u", "v", "key", "XDSegID"]).any()


def test_answer_key(run):
    if run["area"] != "downtown":
        pytest.skip("the answer key covers downtown")
    if not ANSWER_KEY.is_file():
        pytest.skip(f"no reviewed answer key at {ANSWER_KEY}")
    key = pd.read_csv(ANSWER_KEY, dtype={"expected_edges": str}, keep_default_na=False)
    result = run["result"]
    for xd, category, expected in zip(key["XDSegID"], key["category"], key["expected_edges"]):
        want = {tuple(int(n) for n in edge.split("-")) for edge in expected.split(";") if edge}
        rows = result[result["XDSegID"] == xd]
        got = set(zip(rows["u"], rows["v"], rows["key"]))
        assert got == want, f"XD {xd} ({category}): expected {sorted(want)}, got {sorted(got)}"
