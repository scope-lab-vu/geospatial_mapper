"""Measures for judging a matching result, shared by the tests, the comparison script and notebooks.

Every measure works from geometry alone (the result's osm_geom and the XDs' own lines), so the
output of any package version, including 0.2.0, which has no overlap_m or source columns, is
scored the same way.
"""
import geopandas as gpd
import osmnx as ox
import pandas as pd

import geo_mapper_api.converters as gma
from geo_mapper_api import filters, matching

PAIR = matching.PAIR
COVERED_SHARE = 0.9
# An XD has an OSM road nearby when this share of its length lies within band_half_width_m of the
# OSM network. XDs without one are OSM data gaps no matcher can cover, and are reported apart.
ROAD_NEARBY_SHARE = 0.9


def along_table(result, xds, config):
    """For each result row: how far its edge runs along its XD, the intervals, and the XD's needs.

    result: osm_geom in EPSG:4326 and int64 XDSegID; xds: the area's XDs from the loader.
    """
    utm = xds.estimate_utm_crs()
    xd_lines = dict(zip(xds["XDSegID"], xds.to_crs(utm).geometry))
    edges = gpd.GeoSeries(list(result["osm_geom"]), crs=4326).to_crs(utm)
    rows = []
    for (u, v, key, xd), edge in zip(result[PAIR].itertuples(index=False), edges):
        xd_line = xd_lines[xd]
        along, intervals, _ = matching.band_piece(edge, xd_line, config.band_half_width_m)
        rows.append({"u": u, "v": v, "key": key, "XDSegID": xd, "along_m": along, "intervals": intervals,
                     "xd_length_m": xd_line.length,
                     "required_m": matching.required_overlap_m(xd_line.length, config.min_overlap_m)})
    return pd.DataFrame(rows, columns=PAIR + ["along_m", "intervals", "xd_length_m", "required_m"])


def touch_only(table):
    """Rows whose edge reaches less far along its XD than the runs-along rule requires."""
    return table["along_m"] < table["required_m"] - matching.TOLERANCE_M


def xd_coverage_table(table, xds):
    """Coverage of every XD in the area by its matched edges (0 for unmatched XDs)."""
    intervals = table.groupby("XDSegID")["intervals"].sum().to_dict() if len(table) else {}
    lengths = dict(zip(xds["XDSegID"], xds.to_crs(xds.estimate_utm_crs()).geometry.length))
    return pd.DataFrame({"XDSegID": list(lengths), "xd_length_m": list(lengths.values()),
                         "coverage": [matching.xd_coverage(length, intervals.get(xd, []))
                                      for xd, length in lengths.items()]})


def interstate_crossings(xds):
    """Pairs of an interstate XD and a non-interstate XD whose lines cross."""
    named = xds["RoadName"].map(matching.is_interstate)
    interstates, others = xds[named], xds[~named]
    joined = gpd.sjoin(interstates[["XDSegID", "geometry"]], others[["XDSegID", "geometry"]],
                       predicate="crosses", lsuffix="interstate", rsuffix="other")
    return pd.DataFrame({"interstate": joined["XDSegID_interstate"].to_numpy(),
                         "other": joined["XDSegID_other"].to_numpy()})


def area_edges(xds, config):
    """The area's OSM edges (indexed by u, v, key), from the same (cached) download matching uses."""
    polygon = gma.get_exterior_polygon(xds, config.hull_buffer_m)
    return ox.graph_to_gdfs(gma.get_osm_graph(polygon, filters.base_filter), nodes=False)


def road_nearby(xds, edges, config):
    """XDSegIDs with an OSM road within band_half_width_m along at least ROAD_NEARBY_SHARE of their length."""
    utm = xds.estimate_utm_crs()
    reach = edges.to_crs(utm).union_all().buffer(config.band_half_width_m)
    lines = xds.to_crs(utm).geometry
    return {xd for xd, line in zip(xds["XDSegID"], lines)
            if line.intersection(reach).length >= ROAD_NEARBY_SHARE * line.length}


def wrong_level(result, xds, crossings, highway):
    """(wrong, unresolved): crossing XDs' rows on an edge the road-level rule refuses.

    highway: the road types of the graph the result was matched against, indexed by (u, v, key).
    Rows whose edge isn't in it are counted as unresolved, never as fine.
    """
    crossing_xds = set(crossings["interstate"]) | set(crossings["other"])
    info = xds.set_index("XDSegID")
    wrong = unresolved = 0
    for u, v, key, xd in result[PAIR].itertuples(index=False):
        if xd not in crossing_xds:
            continue
        if (u, v, key) not in highway.index:
            unresolved += 1
        elif not matching.road_level_allows(info.at[xd, "RoadName"], info.at[xd, "FRC"], highway.loc[(u, v, key)]):
            wrong += 1
    return wrong, unresolved


def summary(result, xds, edges, config, highway=None):
    """The measures used to judge and compare matching results, as a dict.

    edges: the area's OSM road edges from area_edges, which decide which XDs have a road nearby.
    highway: the road types of the graph the result was matched against, for the wrong-level check;
    defaults to edges["highway"]. A version with another filter passes its own graph's here.
    """
    table = along_table(result, xds, config)
    coverage = xd_coverage_table(table, xds)
    matched = set(result["XDSegID"])
    nearby = road_nearby(xds, edges, config)
    judged = coverage[coverage["XDSegID"].isin(matched & nearby)]["coverage"]
    crossings = interstate_crossings(xds)
    wrong, unresolved = wrong_level(result, xds, crossings, edges["highway"] if highway is None else highway)
    return {
        "edge-XD pairs": len(result.drop_duplicates(PAIR)),
        "matched XDs": len(matched),
        "unmatched XDs": len(xds) - len(matched),
        "XDs with no OSM road nearby": len(xds) - len(nearby),
        "touch-only edges": int(touch_only(table).sum()),
        "matched XDs with a road nearby": len(judged),
        f"of those, >= {COVERED_SHARE:.0%} covered": int((judged >= COVERED_SHARE).sum()),
        "interstate crossings checked": len(crossings),
        "wrong-level matches": wrong,
        "wrong-level rows unresolved": unresolved,
        "geometry-source pairs": int((result["source"] == "geometry").sum()) if "source" in result else 0,
    }
