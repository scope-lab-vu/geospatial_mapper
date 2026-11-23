import concurrent.futures
import logging
import os

import osmnx as ox
import pandas as pd

import geo_mapper_api.converters as gma
import geo_mapper_api.filters as filters
from geo_mapper_api import matching
from geo_mapper_api.config import MatchConfig

logger = logging.getLogger(__name__)

RESULT_COLUMNS = ["u", "v", "key", "osmid", "highway", "XDSegID", "osm_geom", "inrix_geom",
                  "overlap_m", "source", "county"]


def _empty_result():
    types = dict(matching.MATCH_TYPES, county="object")
    return pd.DataFrame({c: pd.Series(dtype=types.get(c, "object")) for c in RESULT_COLUMNS})


def process_county(conflation, inrix_df, county_name, config):
    """Map one county's XD segments to the OSM edges that run along them."""
    county_df = inrix_df[inrix_df["County"] == county_name].copy()
    county_conflation = conflation[conflation["XDSegID"].isin(county_df["XDSegID"])]
    logger.debug("%s: %d XDs, %d conflation rows", county_name, len(county_df), len(county_conflation))

    polygon = gma.get_exterior_polygon(county_df, config.hull_buffer_m)
    G = gma.get_osm_graph(polygon, filters.base_filter)
    edges = ox.graph_to_gdfs(G, nodes=False, edges=True)
    logger.debug("%s: OSM graph has %d edges", county_name, len(edges))

    result = matching.match_county(edges, county_conflation, county_df, config)
    if result.empty:
        logger.warning("%s: no edges matched any XD", county_name)
        return _empty_result()
    logger.info("%s: %d edge-XD pairs for %d XDs", county_name, len(result), result["XDSegID"].nunique())
    return result.assign(county=county_name)[RESULT_COLUMNS]


def _run_county(conflation, inrix_df, county_name, config):
    try:
        return process_county(conflation, inrix_df, county_name, config)
    except Exception as exc:
        raise RuntimeError(f"county {county_name!r} failed: {type(exc).__name__}: {exc}") from exc


def _check_config(config):
    if config is None:
        return MatchConfig()
    if not isinstance(config, MatchConfig):
        raise TypeError(f"config must be a MatchConfig, got {type(config).__name__} {config!r}")
    return config


def _load_inputs(geojson_path, csv_path, counties):
    """Check and load both input files before any county work starts."""
    counties = gma.normalize_counties(counties)
    inrix_df = gma.load_and_prepare_inrix_data_for_multiple_counties(geojson_path, counties)
    conflation = gma.load_conflation(csv_path, inrix_df["XDSegID"])
    return counties, inrix_df, conflation


def _worker_count(n_counties, reserved_cores, cpu_count=None):
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(1, min(n_counties, cpus - reserved_cores))


def _combine(results):
    frames = [r for r in results if not r.empty]
    if not frames:
        return _empty_result()
    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values(["county", "XDSegID", "u", "v", "key"]).reset_index(drop=True)


def serial(geojson_path, csv_path, counties, config=None):
    """Map INRIX XD segments to OSM edges, one county at a time.

    Args:
        geojson_path: INRIX map release GeoJSON (EPSG:4326) with County, XDSegID, FRC and RoadName columns.
        csv_path: INRIX OSM conflation CSV with XDSegID, OSMWayIDs and OSMWayDirections columns.
        counties: a county name or a list of them, matched case-insensitively.
        config: a MatchConfig with the matching settings; None uses the defaults
            (band_half_width_m, min_overlap_m, fallback_below_coverage, hull_buffer_m, reserved_cores).

    Returns:
        A DataFrame with RESULT_COLUMNS, one row per (u, v, key, XDSegID). Geometries are in
        EPSG:4326. overlap_m is how far the edge runs along the XD, in meters. source is
        "conflation" for matches through INRIX's way IDs and "geometry" for the fallback.
        highway is the edge's OSM road types, sorted and joined with ";". osmid is one of the
        edge's ways, so join on u, v and key, not osmid.

    Raises:
        TypeError: config is not a MatchConfig.
        ValueError: a bad input file or county list, before any county is processed.
        RuntimeError: a county failed; the message names the county and the original error.
    """
    config = _check_config(config)
    counties, inrix_df, conflation = _load_inputs(geojson_path, csv_path, counties)
    results = []
    for county_name in counties:
        logger.info("processing county %s", county_name)
        results.append(_run_county(conflation, inrix_df, county_name, config))
    return _combine(results)


def parallel(geojson_path, csv_path, counties, config=None):
    """Same as serial, with counties processed in separate processes.

    Uses every core but config.reserved_cores, and no more workers than counties. Call it under
    `if __name__ == "__main__":`, because worker processes re-import the calling module.
    Arguments, result and errors are the same as serial.
    """
    config = _check_config(config)
    counties, inrix_df, conflation = _load_inputs(geojson_path, csv_path, counties)
    results = []
    workers = _worker_count(len(counties), config.reserved_cores)
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_county, conflation, inrix_df, c, config) for c in counties]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                executor.shutdown(cancel_futures=True)
                raise
    return _combine(results)
