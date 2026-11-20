import warnings
from types import SimpleNamespace

import geopandas as gpd
import osmnx as ox
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

import geo_mapper_api.converters as gma

# 2.695e-5 degrees of latitude is about 3.0 m.
OFFSET_DEG = 2.695e-5

NODES = {
    1: (-86.7800, 36.1600),  # A, west end of Main St
    2: (-86.7790, 36.1600),  # J, junction with Side St
    3: (-86.7780, 36.1600),  # B, east end of Main St
    4: (-86.7790, 36.1610),  # S, north end of Side St
    11: (-89.2000, 36.0000),  # P, Crockett road start
    12: (-89.2000, 36.0009),  # Q, Crockett road end
}

# (u, v, key, osmid, highway)
EDGES = [
    (1, 2, 0, [100, 101], "primary"),
    (2, 1, 0, [100, 101], "primary"),
    (2, 3, 0, [100, 101], "primary"),
    (3, 2, 0, [100, 101], "primary"),
    (2, 4, 0, 200, "residential"),
    (4, 2, 0, 200, "residential"),
    (11, 12, 0, 300, "residential"),
    (12, 11, 0, 300, "residential"),
]


def write_geojson(path, rows):
    """Write INRIX-like rows (dicts with a geometry key) as a GeoJSON file."""
    gpd.GeoDataFrame(rows, geometry="geometry", crs=4326).to_file(path, driver="GeoJSON")
    return path


def write_conflation(path, rows):
    """Write conflation rows (XDSegID, OSMWayIDs, OSMWayDirections) as a CSV file."""
    df = pd.DataFrame(rows, columns=["XDSegID", "OSMWayIDs", "OSMWayDirections"])
    df["WayStartOffset_m"] = 0
    df["WayEndOffset_m"] = 100
    df["WayStartOffset_percent"] = 0
    df["WayEndOffset_percent"] = 100
    df.to_csv(path, index=False)
    return path


def _tiny_graph():
    nodes = gpd.GeoDataFrame(
        {"x": [x for x, _ in NODES.values()], "y": [y for _, y in NODES.values()]},
        geometry=[Point(xy) for xy in NODES.values()],
        index=pd.Index(list(NODES), name="osmid"),
        crs=4326,
    )
    edges = gpd.GeoDataFrame(
        {"osmid": [e[3] for e in EDGES], "highway": [e[4] for e in EDGES]},
        geometry=[LineString([NODES[u], NODES[v]]) for u, v, *_ in EDGES],
        index=pd.MultiIndex.from_tuples([e[:3] for e in EDGES], names=["u", "v", "key"]),
        crs=4326,
    )
    return ox.graph_from_gdfs(nodes, edges)


@pytest.fixture(autouse=True)
def strict_chained_assignment():
    """Fail any test whose code assigns through a chained or copied DataFrame selection."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", pd.errors.ChainedAssignmentError)
        setting_with_copy = getattr(pd.errors, "SettingWithCopyWarning", None)  # removed in pandas 3
        if setting_with_copy is not None:
            warnings.simplefilter("error", setting_with_copy)
        yield


@pytest.fixture
def tiny_inputs(tmp_path):
    """A hand-made Nashville road network with one Davidson XD and one Crockett XD."""
    a, b = NODES[1], NODES[3]
    p, q = NODES[11], NODES[12]
    geojson = write_geojson(
        tmp_path / "xd.geojson",
        [
            {"County": "DAVIDSON", "XDSegID": 5, "Bearing": "E",
             "geometry": LineString([(a[0], a[1] + OFFSET_DEG), (b[0], b[1] + OFFSET_DEG)])},
            {"County": "CROCKETT", "XDSegID": 6, "Bearing": "O",
             "geometry": LineString([(p[0] + OFFSET_DEG, p[1]), (q[0] + OFFSET_DEG, q[1])])},
        ],
    )
    csv = write_conflation(tmp_path / "conflation.csv", [(5, "100;101", "P;P"), (6, "300", "P")])
    return SimpleNamespace(geojson=geojson, csv=csv, graph=_tiny_graph())


@pytest.fixture
def patched_graph(monkeypatch, tiny_inputs):
    """Replace the only network call with the tiny graph."""
    monkeypatch.setattr(gma, "get_osm_graph", lambda polygon, custom_filter: tiny_inputs.graph)
    return tiny_inputs
