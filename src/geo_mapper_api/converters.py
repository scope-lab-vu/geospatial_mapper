import pandas as pd
import geopandas as gpd
import numpy as np
import osmnx as ox
import logging

logger = logging.getLogger(__name__)

INRIX_REQUIRED = ["County", "XDSegID", "FRC", "RoadName"]
CONFLATION_REQUIRED = ["XDSegID", "OSMWayIDs", "OSMWayDirections"]
FRC_VALUES = range(1, 6)


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
    bad = series[~np.isfinite(out) | (out != out.round())]
    if not bad.empty:
        raise ValueError(f"{path}: {column} has missing or non-integer values: {bad.head(5).tolist()}")
    return out.astype("int64")


def load_and_prepare_inrix_data_for_multiple_counties(geojson_path, county_names):
    """Load INRIX XD segments for the given counties.

    county_names must already be normalised by normalize_counties.
    """
    inrix_df = gpd.read_file(geojson_path)
    _require_columns(inrix_df, INRIX_REQUIRED, geojson_path)
    if inrix_df.crs is None or inrix_df.crs.to_epsg() != 4326:
        raise ValueError(f"{geojson_path}: geometries must be in EPSG:4326, got {inrix_df.crs}")
    inrix_df.replace(to_replace='', value=np.nan, inplace=True)
    filtered_df = inrix_df.loc[inrix_df["County"].isin(county_names)].copy()
    missing = [c for c in county_names if c not in set(filtered_df["County"])]
    if missing:
        raise ValueError(f"counties not found in {geojson_path}: {missing}")
    filtered_df["XDSegID"] = _to_int_ids(filtered_df["XDSegID"], geojson_path, "XDSegID")
    filtered_df["FRC"] = _to_int_ids(filtered_df["FRC"], geojson_path, "FRC")
    bad_frc = filtered_df.loc[~filtered_df["FRC"].isin(FRC_VALUES), "FRC"]
    if not bad_frc.empty:
        raise ValueError(f"{geojson_path}: FRC must be 1 to 5, got {bad_frc.head(5).tolist()}")
    unusable = [not (g is not None and not g.is_empty and g.geom_type == "LineString" and g.length > 0)
                for g in filtered_df.geometry]
    if any(unusable):
        ids = filtered_df.loc[unusable, "XDSegID"].head(5).tolist()
        raise ValueError(f"{geojson_path}: XD geometries must be non-empty LineStrings, not for XDSegID {ids}")
    logger.debug("loaded %d XD segments for %s from %s", len(filtered_df), county_names, geojson_path)
    return filtered_df


def get_exterior_polygon(geodf, buffer_m):
    """Return the convex hull of the geometries, grown by buffer_m meters, in EPSG:4326."""
    utm = geodf.estimate_utm_crs()
    hull = geodf.to_crs(utm).union_all().convex_hull.buffer(buffer_m)
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

def load_conflation(csv_path, xd_ids):
    """Load INRIX conflation rows for the given XDs, one row per (XD, OSM way)."""
    conflation = pd.read_csv(csv_path, dtype={"OSMWayIDs": "string", "OSMWayDirections": "string"})
    _require_columns(conflation, CONFLATION_REQUIRED, csv_path)
    conflation["XDSegID"] = _to_int_ids(conflation["XDSegID"], csv_path, "XDSegID")
    conflation = conflation.loc[conflation["XDSegID"].isin(xd_ids), CONFLATION_REQUIRED].copy()

    no_ways = conflation["OSMWayIDs"].isna()
    if no_ways.any():
        logger.info("%s: dropped %d XDs with no OSM way IDs", csv_path, int(no_ways.sum()))
        conflation = conflation.loc[~no_ways].copy()

    conflation["OSMWayIDs"] = conflation["OSMWayIDs"].str.split(";")
    conflation["OSMWayDirections"] = conflation["OSMWayDirections"].str.split(";")
    # A missing direction gives <NA> lengths, which count as a mismatch.
    id_counts = conflation["OSMWayIDs"].str.len().astype("Int64")
    direction_counts = conflation["OSMWayDirections"].str.len().astype("Int64")
    mismatch = id_counts.ne(direction_counts).fillna(True).astype(bool)
    if mismatch.any():
        ids = conflation.loc[mismatch, "XDSegID"].head(5).tolist()
        raise ValueError(f"{csv_path}: OSMWayIDs and OSMWayDirections differ in length for XDSegID {ids}")

    conflation = conflation.explode(["OSMWayIDs", "OSMWayDirections"])
    conflation["OSMWayIDs"] = _to_int_ids(conflation["OSMWayIDs"], csv_path, "OSMWayIDs")
    logger.debug("loaded %d conflation rows from %s", len(conflation), csv_path)
    return conflation.reset_index(drop=True)
