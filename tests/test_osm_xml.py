import osmnx as ox
import pandas as pd
import pytest
from shapely.geometry import box

import geo_mapper_api.converters as gma
import geo_mapper_api.inrix_to_osm as i2o
from geo_mapper_api import filters

# A road of three blocks (way 1), a second road that joins it (way 2), and ways the download leaves out:
# a footpath (3), a building (4), a road drawn as an area (5) and a road cut off from the rest (6).
NODES = {
    1: (36.1600, -86.7800), 2: (36.1600, -86.7790), 3: (36.1600, -86.7780), 4: (36.1600, -86.7770),
    5: (36.1610, -86.7790), 6: (36.1590, -86.7780), 7: (36.1605, -86.7795), 8: (36.1605, -86.7785),
    9: (36.1650, -86.7700), 10: (36.1650, -86.7690), 11: (36.1620, -86.7790),
}
WAYS = {
    1: ([1, 2, 3, 4], {"highway": "residential"}),
    2: ([2, 5, 11], {"highway": "tertiary"}),
    3: ([3, 6], {"highway": "footway"}),
    4: ([7, 8, 5, 7], {"building": "yes"}),
    5: ([2, 7, 8, 2], {"highway": "pedestrian", "area": "yes"}),
    6: ([9, 10], {"highway": "service"}),
}
EVERYTHING = box(-86.79, 36.15, -86.76, 36.17)


def write_osm_xml(path, ways=WAYS):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="test">']
    for node, (lat, lon) in NODES.items():
        lines.append(f'  <node id="{node}" lat="{lat}" lon="{lon}" version="1" timestamp="2025-07-26T00:00:00Z"/>')
    for way, (nodes, tags) in ways.items():
        lines.append(f'  <way id="{way}" version="1" timestamp="2025-07-26T00:00:00Z">')
        lines += [f'    <nd ref="{node}"/>' for node in nodes]
        lines += [f'    <tag k="{key}" v="{value}"/>' for key, value in tags.items()]
        lines.append("  </way>")
    lines.append("</osm>")
    path.write_text("\n".join(lines))
    return str(path)


def _ways(graph):
    osmids = ox.graph_to_gdfs(graph, nodes=False).osmid.explode()
    return set(osmids.astype(int))


@pytest.mark.parametrize("tags, kept", [
    ({"highway": "residential"}, True),
    ({"highway": "service"}, True),
    ({"highway": "residential", "area": "no"}, True),
    ({"highway": "footway"}, False),
    ({"highway": "pedestrian", "area": "yes"}, False),
    ({"building": "yes"}, False),
    ({}, False),
])
def test_keeps_way_follows_the_download_filter(tags, kept):
    assert filters.keeps_way(tags) is kept


def test_every_excluded_road_type_is_refused():
    for value in filters.EXCLUDED_HIGHWAY:
        assert not filters.keeps_way({"highway": value})


def test_graph_from_xml_keeps_only_connected_roads(tmp_path):
    graph = gma.get_osm_graph_from_xml(write_osm_xml(tmp_path / "roads.osm"), EVERYTHING)

    assert _ways(graph) == {1, 2}  # not the footpath, the building, the area or the cut-off road
    edges = ox.graph_to_gdfs(graph, nodes=False)
    assert set(edges.highway) == {"residential", "tertiary"}
    assert {"oneway", "reversed", "length", "geometry"} <= set(edges.columns)


def test_graph_from_xml_is_simplified_like_the_download(tmp_path):
    graph = gma.get_osm_graph_from_xml(write_osm_xml(tmp_path / "roads.osm"), EVERYTHING)

    # Node 3 only joins the footpath, which is left out before the graph is built, so the road runs
    # through it in one edge, as it does in a download.
    assert 3 not in graph.nodes
    assert graph.has_edge(2, 4)


def test_graph_from_xml_is_cut_to_the_polygon(tmp_path):
    west_of_node_4 = box(-86.79, 36.15, -86.7775, 36.17)

    graph = gma.get_osm_graph_from_xml(write_osm_xml(tmp_path / "roads.osm"), west_of_node_4)

    assert 4 not in graph.nodes
    assert 1 in graph.nodes and 2 in graph.nodes


def test_graph_from_xml_with_no_road_in_the_polygon_is_an_error(tmp_path):
    elsewhere = box(-80.0, 30.0, -79.9, 30.1)

    with pytest.raises(ValueError, match="no road"):
        gma.get_osm_graph_from_xml(write_osm_xml(tmp_path / "roads.osm"), elsewhere)


def _no_download(polygon, custom_filter):
    raise AssertionError("the roads were downloaded although osm_xml was given")


@pytest.mark.parametrize("run", [i2o.serial, i2o.parallel])
def test_osm_xml_replaces_the_download(run, monkeypatch, patched_graph, tmp_path):
    expected = i2o.serial(patched_graph.geojson, patched_graph.csv, ["DAVIDSON", "LAKE"])
    read = []
    osm_xml = write_osm_xml(tmp_path / "roads.osm")
    monkeypatch.setattr(i2o.concurrent.futures, "ProcessPoolExecutor", i2o.concurrent.futures.ThreadPoolExecutor)
    monkeypatch.setattr(gma, "get_osm_graph", _no_download)
    monkeypatch.setattr(gma, "get_osm_graph_from_xml", lambda path, polygon: read.append(path) or patched_graph.graph)

    out = run(patched_graph.geojson, patched_graph.csv, ["DAVIDSON", "LAKE"], osm_xml=osm_xml)

    assert read == [osm_xml, osm_xml]  # once per county
    pd.testing.assert_frame_equal(out, expected)


def test_missing_osm_xml_fails_before_any_county(monkeypatch, patched_graph, tmp_path):
    monkeypatch.setattr(gma, "get_osm_graph", _no_download)

    with pytest.raises(ValueError, match="nowhere.osm"):
        i2o.serial(patched_graph.geojson, patched_graph.csv, "DAVIDSON", osm_xml=str(tmp_path / "nowhere.osm"))
