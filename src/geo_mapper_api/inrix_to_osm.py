import concurrent.futures
import logging
import os

import osmnx as ox
import pandas as pd

import geo_mapper_api.converters as gma
import geo_mapper_api.filters as filters

logger = logging.getLogger(__name__)

RESULT_COLUMNS = ["u", "v", "key", "osmid", "XDSegID", "osm_geom", "inrix_geom",
                  "distance", "within_threshold", "county"]

# parallel() leaves this many cores free for the rest of the machine.
RESERVED_CORES = 2


def _empty_result():
    return pd.DataFrame(columns=RESULT_COLUMNS)


def process_county(conflation, inrix_df, county_name, threshold_distance=25.0):
    """Map one county's XD segments to OSM edges within threshold_distance meters."""
    county_df = inrix_df[inrix_df["County"] == county_name].copy()
    county_conflation = conflation[conflation["XDSegID"].isin(county_df["XDSegID"])]
    logger.debug("%s: %d XDs, %d conflation rows", county_name, len(county_df), len(county_conflation))

    polygon = gma.get_exterior_polygon(county_df)
    G = gma.get_osm_graph(polygon, filters.base_filter)
    nodes, edges = ox.graph_to_gdfs(G, nodes=True, edges=True)
    logger.debug("%s: OSM graph has %d nodes, %d edges", county_name, len(nodes), len(edges))

    merged = gma.merge_and_filter_inrix_osm(edges, county_conflation, county_df)
    logger.debug("%s: %d merged rows", county_name, len(merged))
    if merged.empty:
        logger.warning("%s: no OSM edge carries a conflated way ID", county_name)
        return _empty_result()

    matched = gma.filter_groups_by_match(gma.apply_bearing_classification(merged))
    logger.debug("%s: %d rows after the direction filter", county_name, len(matched))

    overlapping = gma.process_top_groups(matched.groupby(["u", "v", "key"]), nodes)
    logger.debug("%s: %d rows after the overlap step", county_name, len(overlapping))

    proximity = gma.check_osm_inrix_proximity(overlapping, threshold_distance)
    within = proximity[proximity["within_threshold"]]
    logger.debug("%s: %d rows within %s m", county_name, len(within), threshold_distance)
    if within.empty:
        logger.warning("%s: no edge within %s m of its XD", county_name, threshold_distance)
        return _empty_result()

    result = (within.sort_values("osmid", kind="stable")
                    .drop_duplicates(["u", "v", "key", "XDSegID"])
                    .assign(county=county_name))
    logger.info("%s: %d edge-XD pairs for %d XDs", county_name, len(result), result["XDSegID"].nunique())
    return result[RESULT_COLUMNS]


def _run_county(conflation, inrix_df, county_name, threshold_distance):
    try:
        return process_county(conflation, inrix_df, county_name, threshold_distance)
    except Exception as exc:
        raise RuntimeError(f"county {county_name!r} failed: {type(exc).__name__}: {exc}") from exc


def _load_inputs(geojson_path, csv_path, counties):
    """Check and load both input files before any county work starts."""
    counties = gma.normalize_counties(counties)
    inrix_df = gma.load_and_prepare_inrix_data_for_multiple_counties(geojson_path, counties)
    conflation = gma.load_conflation(csv_path, inrix_df["XDSegID"])
    return counties, inrix_df, conflation


def _worker_count(n_counties, cpu_count=None):
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(1, min(n_counties, cpus - RESERVED_CORES))


def _combine(results):
    frames = [r for r in results if not r.empty]
    if not frames:
        return _empty_result()
    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values(["county", "XDSegID", "u", "v", "key"]).reset_index(drop=True)


def serial(geojson_path, csv_path, counties, threshold_distance=25.0):
    """Map INRIX XD segments to OSM edges, one county at a time.

    Args:
        geojson_path: INRIX map release GeoJSON with County, XDSegID and Bearing columns.
        csv_path: INRIX OSM conflation CSV with XDSegID, OSMWayIDs and OSMWayDirections columns.
        counties: a county name or a list of them, matched case-insensitively.
        threshold_distance: the largest distance in meters between an edge and its XD.

    Returns:
        A DataFrame with RESULT_COLUMNS, one row per (u, v, key, XDSegID). Geometries are in
        EPSG:4326 and distance is in meters.

    Raises:
        ValueError: a bad input file or county list, before any county is processed.
        RuntimeError: a county failed; the message names the county and the original error.
    """
    counties, inrix_df, conflation = _load_inputs(geojson_path, csv_path, counties)
    results = []
    for county_name in counties:
        logger.info("processing county %s", county_name)
        results.append(_run_county(conflation, inrix_df, county_name, threshold_distance))
    return _combine(results)


def parallel(geojson_path, csv_path, counties, threshold_distance=25.0):
    """Same as serial, with counties processed in separate processes.

    Uses every core but RESERVED_CORES, and no more workers than counties. Call it under
    `if __name__ == "__main__":`, because worker processes re-import the calling module.
    Arguments, result and errors are the same as serial.
    """
    counties, inrix_df, conflation = _load_inputs(geojson_path, csv_path, counties)
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=_worker_count(len(counties))) as executor:
        futures = [executor.submit(_run_county, conflation, inrix_df, c, threshold_distance) for c in counties]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                executor.shutdown(cancel_futures=True)
                raise
    return _combine(results)
