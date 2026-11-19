import logging
import math
import warnings

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd

logger = logging.getLogger(__name__)

INRIX_REQUIRED = ["County", "XDSegID", "Bearing"]
CONFLATION_REQUIRED = ["XDSegID", "OSMWayIDs", "OSMWayDirections"]

# INRIX bearing -> OSM compass directions that count as the same direction of travel.
ALLOWED_COMPASS = {
    "N": {"NW", "N", "NE"},
    "E": {"NE", "E", "SE"},
    "S": {"SE", "S", "SW"},
    "W": {"SW", "W", "NW"},
}

OUTPUT_COLUMNS = ["u", "v", "key", "osmid", "XDSegID", "geometry", "geometry_inrix",
                  "osm_line_bearing", "Bearing", "compass_direction"]
PROXIMITY_COLUMNS = ["u", "v", "key", "osmid", "XDSegID", "osm_geom", "inrix_geom",
                     "distance", "within_threshold"]

# Buffers in degrees, used by the overlap step. The matching plan replaces them with meters.
OSM_EDGE_BUFFER_DEG = 1e-4         # about 11 m N-S, 9 m E-W at 36 N
INRIX_BUFFER_DEG = 1e-6            # about 0.1 m
INRIX_FALLBACK_BUFFER_DEG = 1e-4   # about 10 m
NODE_BUFFER_DEG = 1e-3             # about 100 m
HULL_BUFFER_M = 200


def normalize_counties(counties):
    """Return county names stripped, upper-cased and de-duplicated, keeping first-seen order."""
    names = [counties] if isinstance(counties, str) else list(counties)
    if not names or not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError(f"counties must be a non-empty list of county names, got {counties!r}")
    return list(dict.fromkeys(n.strip().upper() for n in names))


def _require_columns(df, required, path):
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing required columns {missing}")


def _to_int_ids(series, path, column):
    """Parse IDs as int64, or raise naming the file, the column and up to five bad values."""
    out = pd.to_numeric(series, errors="coerce")
    bad = series[out.isna() | (out != out.round())]
    if not bad.empty:
        raise ValueError(f"{path}: {column} has missing or non-integer values: {bad.head(5).tolist()}")
    return out.astype("int64")


def load_and_prepare_inrix_data_for_multiple_counties(geojson_path, county_names):
    """Load INRIX XD segments for the given counties.

    county_names must already be normalised by normalize_counties.
    """
    inrix_df = gpd.read_file(geojson_path, engine="fiona")
    _require_columns(inrix_df, INRIX_REQUIRED, geojson_path)
    if inrix_df.crs is None or inrix_df.crs.to_epsg() != 4326:
        raise ValueError(f"{geojson_path}: geometries must be in EPSG:4326, got {inrix_df.crs}")
    inrix_df.replace(to_replace='', value=np.nan, inplace=True)
    filtered_df = inrix_df.loc[inrix_df["County"].isin(county_names)].copy()
    missing = [c for c in county_names if c not in set(filtered_df["County"])]
    if missing:
        raise ValueError(f"counties not found in {geojson_path}: {missing}")
    filtered_df["XDSegID"] = _to_int_ids(filtered_df["XDSegID"], geojson_path, "XDSegID")
    logger.debug("loaded %d XD segments for %s from %s", len(filtered_df), county_names, geojson_path)
    return filtered_df


def get_exterior_polygon(geodf):
    """Return the convex hull of the geometries, grown by HULL_BUFFER_M meters, in EPSG:4326."""
    utm = geodf.estimate_utm_crs()
    hull = geodf.to_crs(utm).unary_union.convex_hull.buffer(HULL_BUFFER_M)
    return gpd.GeoSeries([hull], crs=utm).to_crs(4326).iloc[0]


def get_osm_graph(polygon, custom_filter):
    """
    Get OSM road network graph for the given polygon with the custom filter.
    
    Parameters:
    polygon (shapely.geometry.Polygon): The polygon defining the area of interest.
    custom_filter (str): Custom filter to apply to the OSM data.
    
    Returns:
    networkx.MultiDiGraph: The road network graph for the given polygon.
    """
    G = ox.graph_from_polygon(polygon, custom_filter=custom_filter, network_type='drive')
    return G

def classify_bearing(bearing):
    if bearing >= 337.5 or bearing < 22.5:
        return 'N'
    elif 22.5 <= bearing < 67.5:
        return 'NE'
    elif 67.5 <= bearing < 112.5:
        return 'E'
    elif 112.5 <= bearing < 157.5:
        return 'SE'
    elif 157.5 <= bearing < 202.5:
        return 'S'
    elif 202.5 <= bearing < 247.5:
        return 'SW'
    elif 247.5 <= bearing < 292.5:
        return 'W'
    elif 292.5 <= bearing < 337.5:
        return 'NW'
    

def calculate_bearing(point1, point2):
    lon1, lat1 = point1
    lon2, lat2 = point2
    dLon = math.radians(lon2 - lon1)
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    x = math.sin(dLon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dLon))
    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360
    return compass_bearing

def line_bearing(line):
    point1 = (line.coords[0][0], line.coords[0][1])
    point2 = (line.coords[-1][0], line.coords[-1][1])
    return calculate_bearing(point1, point2)
    

def load_conflation(csv_path, xd_ids):
    """Load INRIX conflation rows for the given XDs, one row per (XD, OSM way)."""
    conflation = pd.read_csv(csv_path, dtype={"OSMWayIDs": str, "OSMWayDirections": str})
    _require_columns(conflation, CONFLATION_REQUIRED, csv_path)
    conflation["XDSegID"] = _to_int_ids(conflation["XDSegID"], csv_path, "XDSegID")
    conflation = conflation.loc[conflation["XDSegID"].isin(xd_ids), CONFLATION_REQUIRED].copy()

    no_ways = conflation["OSMWayIDs"].isna()
    if no_ways.any():
        logger.info("%s: dropped %d XDs with no OSM way IDs", csv_path, int(no_ways.sum()))
        conflation = conflation.loc[~no_ways].copy()

    conflation["OSMWayIDs"] = conflation["OSMWayIDs"].str.split(";")
    conflation["OSMWayDirections"] = conflation["OSMWayDirections"].str.split(";")
    mismatch = conflation["OSMWayIDs"].str.len() != conflation["OSMWayDirections"].str.len()
    if mismatch.any():
        ids = conflation.loc[mismatch, "XDSegID"].head(5).tolist()
        raise ValueError(f"{csv_path}: OSMWayIDs and OSMWayDirections differ in length for XDSegID {ids}")

    conflation = conflation.explode(["OSMWayIDs", "OSMWayDirections"])
    conflation["OSMWayIDs"] = _to_int_ids(conflation["OSMWayIDs"], csv_path, "OSMWayIDs")
    logger.debug("loaded %d conflation rows from %s", len(conflation), csv_path)
    return conflation.reset_index(drop=True)


def merge_and_filter_inrix_osm(edges, conflation, inrix_df):
    """Join OSM edges to XD segments through the conflation's OSM way IDs."""
    osm = edges.reset_index()[["u", "v", "key", "osmid", "geometry"]].explode("osmid")
    osm["osmid"] = osm["osmid"].astype("int64")
    inrix = pd.DataFrame(inrix_df[["XDSegID", "Bearing"]]).assign(geometry_inrix=inrix_df.geometry.values)
    return (osm.merge(conflation, left_on="osmid", right_on="OSMWayIDs", how="inner")
               .merge(inrix, on="XDSegID", how="inner"))


def apply_bearing_classification(df):
    """Add the OSM edge bearing, its compass direction, and whether it agrees with the XD bearing."""
    df = df.copy()
    df["osm_line_bearing"] = df["geometry"].apply(line_bearing)
    df["compass_direction"] = df["osm_line_bearing"].apply(classify_bearing)
    # A list, not a row-wise apply: on an empty frame apply(axis=1) returns a DataFrame.
    df["match"] = [c in ALLOWED_COMPASS[b] if b in ALLOWED_COMPASS else True
                   for c, b in zip(df["compass_direction"], df["Bearing"])]
    outside = df.loc[~df["Bearing"].isin(list(ALLOWED_COMPASS)), "XDSegID"].nunique()
    if outside:
        logger.info("%d XDs have a bearing outside N/E/S/W and keep all candidate edges", outside)
    return df


def filter_groups_by_match(df):
    """Keep rows whose (osmid, XDSegID) group has one row, or whose direction matches."""
    size = df.groupby(["osmid", "XDSegID"])["XDSegID"].transform("size")
    return df[(size == 1) | df["match"]].copy()


def buffer_geometries(gdf, column_name, buffer_size):
    """
    Applies a buffer to the geometries in a GeoDataFrame.
    
    Parameters:
    - gdf: GeoDataFrame containing the geometries.
    - column_name: The name of the geometry column to buffer.
    - buffer_size: The buffer size to apply.
    
    Returns:
    - GeoDataFrame with buffered geometries.
    """
    buffered_gdf = gdf.copy()
    # Buffers here are in degrees on purpose until the matching plan moves them to meters,
    # so only geopandas' warning about that is silenced.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS", category=UserWarning)
        buffered_gdf[column_name] = buffered_gdf[column_name].buffer(buffer_size)
    return buffered_gdf


def _rows_for_xds(group_df, xd_ids):
    rows = group_df[group_df.XDSegID.isin(xd_ids)][OUTPUT_COLUMNS]
    return rows.drop_duplicates().reset_index(drop=True)


def _fallback_overlap(group_df, buffered_osm, gdf_inrix):
    """Keep the XDs whose slightly buffered line meets the buffered OSM edge."""
    buffered_inrix = buffer_geometries(gdf_inrix, 'geometry_inrix_copy', INRIX_FALLBACK_BUFFER_DEG)
    joined = gpd.sjoin(buffered_osm, buffered_inrix, how='inner', predicate='intersects')
    return _rows_for_xds(group_df, joined.XDSegID)


def find_overlapping_geometries(group_df,gdf_osm, gdf_inrix, gdf_nodes):
    """
    Finds overlapping geometries between OSM, INRIX, and nodes data.
    
    Parameters:
    - gdf_osm: GeoDataFrame containing OSM data.
    - gdf_inrix: GeoDataFrame containing INRIX data.
    - gdf_nodes: GeoDataFrame containing nodes data.
    
    Returns:
    - DataFrame of overlapping geometries.
    """
    buffered_osm = buffer_geometries(gdf_osm, 'geometry', OSM_EDGE_BUFFER_DEG)
    buffered_inrix = buffer_geometries(gdf_inrix, 'geometry_inrix_copy', INRIX_BUFFER_DEG)
    buffered_nodes = buffer_geometries(gdf_nodes, 'geometry', NODE_BUFFER_DEG)

    overlapping_nodes = gpd.sjoin(buffered_nodes, buffered_inrix, how='inner', predicate='intersects')
    if len(overlapping_nodes) == 0:
        return _fallback_overlap(group_df, buffered_osm, gdf_inrix)

    overlapping_nodes = overlapping_nodes.drop_duplicates().reset_index(drop=True)
    pairs = pd.merge(overlapping_nodes[['osmid', 'XDSegID']], overlapping_nodes[['osmid', 'XDSegID']],
                     on='XDSegID', how='inner', suffixes=('_u', '_v'))
    pairs = pairs[pairs['osmid_u'] != pairs['osmid_v']]
    if len(pairs) == 0:
        return _fallback_overlap(group_df, buffered_osm, gdf_inrix)
    return _rows_for_xds(group_df, pairs.XDSegID)


def process_top_groups(grouped_data, nodes):
    """
    Processes the top groups and finds overlapping geometries.
    
    Parameters:
    - grouped_data: Grouped DataFrame containing the top groups.
    - nodes: GeoDataFrame containing nodes data.
    
    Returns:
    - Concatenated DataFrame of all overlapping geometries.
    """
    all_overlapping_geometries = []
    
    for group_name, group_df in grouped_data:
        gdf_osm = gpd.GeoDataFrame(group_df.drop(columns=['XDSegID', 'geometry_inrix'], axis=1), geometry='geometry')
        gdf_inrix = gpd.GeoDataFrame(group_df[['XDSegID', 'geometry_inrix']], geometry='geometry_inrix')
        gdf_inrix['geometry_inrix_copy'] = gdf_inrix['geometry_inrix']
        gdf_inrix.set_geometry('geometry_inrix_copy', inplace=True)
        gdf_nodes = gpd.GeoDataFrame(nodes[nodes.index.isin([group_name[0], group_name[1]])].reset_index(), geometry='geometry')
        
        overlapping_geometries = find_overlapping_geometries(group_df,gdf_osm, gdf_inrix, gdf_nodes)
        all_overlapping_geometries.append(overlapping_geometries)
    
    if not all_overlapping_geometries:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    return pd.concat(all_overlapping_geometries, ignore_index=True)


def check_osm_inrix_proximity(df, threshold_m):
    """
    Measure the closest distance in meters between each OSM edge and its INRIX segment.

    Distances are computed in the local UTM zone. Returned geometries stay in EPSG:4326.
    """
    if df.empty:
        return pd.DataFrame(columns=PROXIMITY_COLUMNS)
    osm = gpd.GeoSeries(df["geometry"].values, crs=4326)
    inrix = gpd.GeoSeries(df["geometry_inrix"].values, crs=4326)
    utm = osm.estimate_utm_crs()
    distance = osm.to_crs(utm).distance(inrix.to_crs(utm), align=False).values
    return pd.DataFrame({
        "u": df["u"].values,
        "v": df["v"].values,
        "key": df["key"].values,
        "osmid": df["osmid"].values,
        "XDSegID": df["XDSegID"].values,
        "osm_geom": osm.values,
        "inrix_geom": inrix.values,
        "distance": distance,
        "within_threshold": distance <= threshold_m,
    })
