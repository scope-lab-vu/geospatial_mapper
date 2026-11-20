"""Rules that decide which OSM edges make up each INRIX XD segment.

Every distance here is in meters, so geometries must be in a projected CRS (the county's UTM
zone). An edge belongs to an XD when it runs along the XD, in the XD's direction of travel:

- The band is the area within `band_half_width_m` of the XD line, cut square at its two ends.
- An edge's extent is how far along the XD its part inside the band reaches. A cross street,
  or the next block past the XD's end, reaches almost nowhere.
- Direction comes from geometry: the edge and the XD must point the same way where they meet.
  INRIX's P/N flag is compared with it on two-way edges and disagreements are logged.
- XDs left mostly uncovered (out-of-date way IDs) are searched again by geometry alone, with a
  road-level rule so an XD can't match a road on another level.
"""
import logging
import re
from collections import Counter

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

logger = logging.getLogger(__name__)

INTERSTATE_NAME = re.compile(r"^I-\d")
MOTORWAY = "motorway"
INTERSTATE_EDGES = {"motorway", "motorway_link"}

# Geometry constants (not settings): how a seam crossing is told apart from an ordinary segment,
# the step used to read a line's local direction, and the rounding tolerance of the keep test.
SEAM_SHARE_OF_XD = 0.5
SEAM_SEGMENT_RATIO = 2.0
TANGENT_STEP_M = 1.0
TOLERANCE_M = 1e-6

PAIR = ["u", "v", "key", "XDSegID"]
MATCH_COLUMNS = ["u", "v", "key", "osmid", "XDSegID", "osm_geom", "inrix_geom", "overlap_m", "source"]


def band(xd_line, half_width_m):
    """The area within half_width_m of the XD line, cut square at the XD's ends."""
    return xd_line.buffer(half_width_m, cap_style="flat")


def _line_parts(geom):
    """The non-zero-length LineStrings inside an intersection result."""
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if geom.length > 0 else []
    if hasattr(geom, "geoms"):
        return [part for g in geom.geoms for part in _line_parts(g)]
    return []


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _intervals_along(part, xd_line):
    """Positions along the XD that one band part spans, one interval per segment."""
    length = xd_line.length
    coords = list(part.coords)
    positions = [xd_line.project(Point(c)) for c in coords]
    intervals = []
    for (a, b), (pa, pb) in zip(zip(coords, coords[1:]), zip(positions, positions[1:])):
        low, high = min(pa, pb), max(pa, pb)
        segment = Point(a).distance(Point(b))
        if high - low > SEAM_SHARE_OF_XD * length and high - low > SEAM_SEGMENT_RATIO * segment:
            # The segment crosses the start/end node of a closed XD such as a roundabout.
            intervals += [(high, length), (0.0, low)]
        else:
            intervals.append((low, high))
    return intervals


def _tangent(line, position):
    a = line.interpolate(max(position - TANGENT_STEP_M, 0.0))
    b = line.interpolate(min(position + TANGENT_STEP_M, line.length))
    return b.x - a.x, b.y - a.y


def _runs_with(edge_line, part, xd_line):
    """True when the edge points the XD's way at the middle of its longest band part."""
    middle = part.interpolate(0.5, normalized=True)
    ex, ey = _tangent(edge_line, edge_line.project(middle))
    xx, xy = _tangent(xd_line, xd_line.project(middle))
    return ex * xx + ey * xy > 0


def band_piece(edge_line, xd_line, half_width_m):
    """(along_m, intervals, runs_with) for one edge against one XD, from one intersection."""
    parts = _line_parts(edge_line.intersection(band(xd_line, half_width_m)))
    if not parts:
        return 0.0, [], False
    intervals = _merge([interval for part in parts for interval in _intervals_along(part, xd_line)])
    along = sum(end - start for start, end in intervals)
    longest = max(parts, key=lambda part: part.length)
    return along, intervals, bool(_runs_with(edge_line, longest, xd_line))


def required_overlap_m(xd_length_m, min_overlap_m):
    """How far an edge must run along an XD: min_overlap_m, or half a short XD's length."""
    return min(min_overlap_m, xd_length_m / 2)


def _tags(highway):
    return set(highway) if isinstance(highway, list) else {highway}


def road_level_allows(road_name, frc, highway):
    """Whether a fallback match may put this XD on an edge with these OSM highway tags."""
    tags = _tags(highway)
    if isinstance(road_name, str) and INTERSTATE_NAME.match(road_name):
        return bool(tags & INTERSTATE_EDGES)
    if frc <= 2:
        return True
    return MOTORWAY not in tags


def _is_bool(value):
    return isinstance(value, (bool, np.bool_))


def is_two_way(oneway, reversed_):
    """True for an edge whose osmnx `reversed` flag follows its way's drawn direction."""
    return bool(_is_bool(oneway) and not oneway and _is_bool(reversed_))


def pn_runs_with(direction, reversed_):
    """INRIX's verdict on a two-way edge: P means the way's drawn direction, N the opposite."""
    return (direction == "P") == (not bool(reversed_))


def xd_coverage(xd_length_m, intervals):
    """Share of the XD's length covered by the union of the intervals."""
    if xd_length_m <= 0 or not intervals:
        return 0.0
    covered = sum(min(end, xd_length_m) - max(start, 0.0) for start, end in _merge(intervals))
    return covered / xd_length_m


def _smallest_way(osmid):
    return int(min(osmid)) if isinstance(osmid, list) else int(osmid)


def _measure(rows, config):
    """Add along_m, intervals, runs_with and required_m to candidate rows."""
    pieces = [band_piece(e, x, config.band_half_width_m) for e, x in zip(rows["edge_line"], rows["xd_line"])]
    rows = rows.copy()
    rows["along_m"] = [p[0] for p in pieces]
    rows["intervals"] = [p[1] for p in pieces]
    rows["runs_with"] = [p[2] for p in pieces]
    rows["required_m"] = [required_overlap_m(x.length, config.min_overlap_m) for x in rows["xd_line"]]
    return rows


def _xd_frame(xds_utm):
    return pd.DataFrame({"XDSegID": xds_utm["XDSegID"].to_numpy(), "FRC": xds_utm["FRC"].to_numpy(),
                         "RoadName": xds_utm["RoadName"].to_numpy(), "xd_line": xds_utm.geometry.to_numpy()})


def way_id_candidates(edges_utm, conflation, xds_utm):
    """Edges carrying a way the conflation lists for an XD, one row per (edge, way, XD)."""
    osm = pd.DataFrame({"u": edges_utm["u"].to_numpy(), "v": edges_utm["v"].to_numpy(),
                        "key": edges_utm["key"].to_numpy(), "osmid": edges_utm["osmid"].to_numpy(),
                        "reversed": edges_utm["reversed"].to_numpy(), "oneway": edges_utm["oneway"].to_numpy(),
                        "highway": edges_utm["highway"].to_numpy(), "edge_line": edges_utm.geometry.to_numpy()})
    osm["edge_osmid"] = osm["osmid"]
    osm = osm.explode("osmid")
    osm["osmid"] = osm["osmid"].astype("int64")
    return (osm.merge(conflation, left_on="osmid", right_on="OSMWayIDs", how="inner")
               .merge(_xd_frame(xds_utm), on="XDSegID", how="inner")
               .reset_index(drop=True))


def keep_along(candidates, config):
    """Measured candidates that run along their XD in its direction."""
    rows = _measure(candidates, config)
    return rows[rows["runs_with"] & (rows["along_m"] >= rows["required_m"] - TOLERANCE_M)]


def geometry_fallback(edges_utm, xds_utm, seen_pairs, config):
    """Edges found by geometry alone for these XDs; returns (rows, rejection counts by reason)."""
    empty = pd.DataFrame(columns=["u", "v", "key", "osmid", "XDSegID", "overlap_m", "intervals"])
    if len(xds_utm) == 0 or len(edges_utm) == 0:
        return empty, {}
    bands = gpd.GeoDataFrame(_xd_frame(xds_utm),
                             geometry=[band(x, config.band_half_width_m) for x in xds_utm.geometry],
                             crs=xds_utm.crs)
    edges = gpd.GeoDataFrame({"u": edges_utm["u"].to_numpy(), "v": edges_utm["v"].to_numpy(),
                              "key": edges_utm["key"].to_numpy(), "osmid": edges_utm["osmid"].to_numpy(),
                              "highway": edges_utm["highway"].to_numpy()},
                             geometry=edges_utm.geometry.to_numpy(), crs=edges_utm.crs)
    hits = gpd.sjoin(edges, bands, how="inner", predicate="intersects")
    hits = pd.DataFrame(hits.drop(columns="index_right")).rename(columns={"geometry": "edge_line"})
    unseen = [(u, v, k, x) not in seen_pairs for u, v, k, x in zip(hits.u, hits.v, hits.key, hits.XDSegID)]
    hits = hits[unseen]
    if hits.empty:
        return empty, {}

    rows = _measure(hits, config)
    reasons = []
    for along, required, runs_with, road_name, frc, highway in zip(
            rows.along_m, rows.required_m, rows.runs_with, rows.RoadName, rows.FRC, rows.highway):
        if along < required - TOLERANCE_M:
            reasons.append("overlap")
        elif not runs_with:
            reasons.append("direction")
        elif not road_level_allows(road_name, frc, highway):
            reasons.append("road_level")
        else:
            reasons.append(None)
    rejected = dict(Counter(r for r in reasons if r is not None))
    kept = rows[[r is None for r in reasons]]
    out = pd.DataFrame({"u": kept["u"].to_numpy(), "v": kept["v"].to_numpy(), "key": kept["key"].to_numpy(),
                        "osmid": [_smallest_way(o) for o in kept["osmid"]],
                        "XDSegID": kept["XDSegID"].to_numpy(), "overlap_m": kept["along_m"].to_numpy(),
                        "intervals": kept["intervals"].to_list()})
    return out, rejected


def match_county(edges, conflation, xds, config):
    """Match one county's XDs to OSM edges; returns (rows in MATCH_COLUMNS, P/N disagreements).

    edges: osmnx edges (EPSG:4326, indexed by u, v, key); conflation: that county's rows from
    load_conflation; xds: that county's XDs (EPSG:4326) with XDSegID, FRC and RoadName.
    """
    utm = xds.estimate_utm_crs()
    edges_4326 = edges.reset_index()
    edges_utm = edges_4326.to_crs(utm)
    xds_utm = xds.to_crs(utm)

    candidates = way_id_candidates(edges_utm, conflation, xds_utm)
    measured = _measure(candidates, config)
    long_enough = measured["along_m"] >= measured["required_m"] - TOLERANCE_M
    logger.debug("%d way-ID candidates for %d XDs; %d run along their XD",
                 len(measured), measured["XDSegID"].nunique(), int(long_enough.sum()))

    two_way = [is_two_way(o, r) for o, r in zip(measured["oneway"], measured["reversed"])]
    pn = [two and pn_runs_with(d, r) for two, d, r in zip(two_way, measured["OSMWayDirections"], measured["reversed"])]
    disagree = measured[long_enough & pd.Series(two_way, index=measured.index)
                        & (pd.Series(pn, index=measured.index) != measured["runs_with"])]
    disagreements = len(disagree.drop_duplicates(PAIR))
    if disagreements:
        logger.warning("P/N disagrees with geometry on %d edge-XD pairs (geometry decides), e.g. XDs %s",
                       disagreements, sorted(disagree["XDSegID"].unique())[:5])

    kept = measured[long_enough & measured["runs_with"]]
    conflation_rows = pd.DataFrame({"u": kept["u"].to_numpy(), "v": kept["v"].to_numpy(), "key": kept["key"].to_numpy(),
                                    "osmid": kept["osmid"].to_numpy(), "XDSegID": kept["XDSegID"].to_numpy(),
                                    "overlap_m": kept["along_m"].to_numpy(), "intervals": kept["intervals"].to_list()})
    logger.debug("%d way-ID rows kept by direction and the runs-along rule", len(conflation_rows))

    intervals_by_xd = conflation_rows.groupby("XDSegID")["intervals"].sum().to_dict() if len(conflation_rows) else {}
    coverage = {xd: xd_coverage(line.length, intervals_by_xd.get(xd, []))
                for xd, line in zip(xds_utm["XDSegID"], xds_utm.geometry)}
    needs_fallback = [xd for xd, share in coverage.items() if share < config.fallback_below_coverage]
    seen = set(map(tuple, candidates[PAIR].itertuples(index=False))) if len(candidates) else set()
    fallback_rows, rejected = geometry_fallback(
        edges_utm, xds_utm[xds_utm["XDSegID"].isin(needs_fallback)], seen, config)
    logger.debug("%d XDs below %.2f coverage searched by geometry: %d rows kept, rejected %s",
                 len(needs_fallback), config.fallback_below_coverage, len(fallback_rows), rejected)

    rows = pd.concat([conflation_rows.assign(source="conflation"), fallback_rows.assign(source="geometry")],
                     ignore_index=True)
    rows["source_rank"] = (rows["source"] != "conflation").astype(int)
    rows = (rows.sort_values(["source_rank", "osmid"], kind="stable")
                .drop_duplicates(PAIR).drop(columns=["source_rank", "intervals"]))

    osm_geom = dict(zip(map(tuple, edges_4326[["u", "v", "key"]].itertuples(index=False)), edges_4326.geometry))
    xd_geom = dict(zip(xds["XDSegID"], xds.geometry))
    rows["osm_geom"] = [osm_geom[(u, v, k)] for u, v, k in zip(rows.u, rows.v, rows.key)]
    rows["inrix_geom"] = [xd_geom[x] for x in rows.XDSegID]
    rows = rows[MATCH_COLUMNS].reset_index(drop=True)

    matched = rows["XDSegID"].nunique()
    logger.info("%d edge-XD pairs: %d XDs matched, %d unmatched, %d searched by geometry, "
                "%d P/N disagreements, fallback rejections %s",
                len(rows), matched, len(xds) - matched, len(needs_fallback), disagreements, rejected,
                extra={"pn_disagreements": disagreements, "fallback_rejections": rejected})
    return rows, disagreements
