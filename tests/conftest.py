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
    5: (-86.7770, 36.1600),  # E2, the next block of Main St past B
    6: (-86.7760, 36.1600),  # F, end of the block after E2
    11: (-89.2000, 36.0000),  # P, Crockett road start
    12: (-89.2000, 36.0009),  # Q, Crockett road end
}

# (u, v, key, osmid, highway, reversed). Every road is two-way: the edge in the way's drawn order has
# reversed=False and its (v, u) twin has reversed=True, as osmnx builds them.
EDGES = [
    (1, 2, 0, [100, 101], "primary", False),
    (2, 1, 0, [100, 101], "primary", True),
    (2, 3, 0, [100, 101], "primary", False),
    (3, 2, 0, [100, 101], "primary", True),
    (3, 5, 0, [100, 101], "primary", False),
    (5, 3, 0, [100, 101], "primary", True),
    (5, 6, 0, 400, "primary", False),
    (6, 5, 0, 400, "primary", True),
    (2, 4, 0, 200, "residential", False),
    (4, 2, 0, 200, "residential", True),
    (11, 12, 0, 300, "residential", False),
    (12, 11, 0, 300, "residential", True),
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
        {"osmid": [e[3] for e in EDGES], "highway": [e[4] for e in EDGES],
         "reversed": [e[5] for e in EDGES], "oneway": [False] * len(EDGES)},
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


def north(a, b):
    """The line a -> b moved about 3 m north."""
    return LineString([(a[0], a[1] + OFFSET_DEG), (b[0], b[1] + OFFSET_DEG)])


def east(a, b):
    """The line a -> b moved about 2.4 m east."""
    return LineString([(a[0] + OFFSET_DEG, a[1]), (b[0] + OFFSET_DEG, b[1])])


def xd_rows(xd9_name="I-99 N"):
    """The tiny network's XDs. Bearing stays for 0.2.0-era code, which required it."""
    return [
        # On Main St from A to B, eastbound; conflated to ways 100 and 101.
        {"County": "DAVIDSON", "XDSegID": 5, "FRC": 4, "RoadName": "ALPHA ROAD", "Bearing": "E",
         "geometry": north(NODES[1], NODES[3])},
        # On a Crockett road, northbound.
        {"County": "CROCKETT", "XDSegID": 6, "FRC": 4, "RoadName": "GAMMA ROAD", "Bearing": "O",
         "geometry": east(NODES[11], NODES[12])},
        # On the block E2 -> F, but conflated to a way that no longer exists (999).
        {"County": "DAVIDSON", "XDSegID": 8, "FRC": 4, "RoadName": "TEST ST", "Bearing": "E",
         "geometry": north(NODES[5], NODES[6])},
        # An FRC 1 XD over the residential Side St, also with a stale way ID.
        {"County": "DAVIDSON", "XDSegID": 9, "FRC": 1, "RoadName": xd9_name, "Bearing": "N",
         "geometry": east(NODES[2], NODES[4])},
    ]


CONFLATION = [(5, "100;101", "P;P"), (6, "300", "P"), (8, "999", "P"), (9, "999", "P")]


@pytest.fixture
def tiny_inputs(tmp_path):
    """A hand-made Nashville road network with Davidson XDs 5, 8 and 9 and Crockett XD 6."""
    geojson = write_geojson(tmp_path / "xd.geojson", xd_rows())
    csv = write_conflation(tmp_path / "conflation.csv", CONFLATION)
    return SimpleNamespace(geojson=geojson, csv=csv, graph=_tiny_graph())


@pytest.fixture
def patched_graph(monkeypatch, tiny_inputs):
    """Replace the only network call with the tiny graph."""
    monkeypatch.setattr(gma, "get_osm_graph", lambda polygon, custom_filter: tiny_inputs.graph)
    return tiny_inputs
