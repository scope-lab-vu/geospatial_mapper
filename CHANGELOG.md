# Changelog

All notable changes to geo-mapper-api are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`osm_xml` on `serial` and `parallel`** reads the roads from an OSM XML file instead of downloading them, so matches can be made on the same roads a routing engine was built from and repeat exactly. The file's ways go through the same road filter as a download. Without `osm_xml` nothing changes.
- **The README shows how to make that file** from a `.osm.pbf` extract with the osmium command-line tool, and how to install the tool.

## [0.3.1] - 2026-11-23

### Fixed
- **The road filter downloaded every OSM way in the county**, including buildings, land use, rivers, power lines and railways, because Overpass's `!~` also matches ways without the tag. The geometric fallback then matched XDs to some of them, such as power lines. The filter now requires a `highway` tag, skips roads drawn as areas, and removes only the exact road types it lists.
- **XDs that INRIX puts on service roads can now be matched.** Service roads (including driveways, parking aisles and alleys), busways, bus guideways, rest areas, service plazas, escape ramps and emergency bays are now downloaded and matched through INRIX's way IDs. osmnx still drops service roads cut off from the rest of the network. Tracks, roads under construction and paths for people stay excluded.

### Changed
- **The geometric fallback matches only general-traffic roads**, which are `motorway`, `trunk`, `primary`, `secondary` and `tertiary` with their `_link` ramps, `unclassified`, `residential`, `living_street` and `road`. Refusals of other road types are logged with the reason `road_type`.
- **`fallback_below_coverage` defaults to 0.9** instead of 0.5. With service roads in the network, driveways split roads into more edges, and pieces of a road that OSM renumbered since INRIX's conflation no longer ride along on a longer edge. Searching every XD below 90% coverage fills those gaps. On downtown Nashville, XDs at least 90% covered went from 1,450 in 0.3.0 to 1,511 of 1,603, with no wrong-level matches.

### Added
- **A `highway` column** with each matched edge's OSM road type, joined with `;` when an edge merges several types. The README shows how to drop service roads or keep only general-traffic roads.
- **The README says to join results on `u`, `v` and `key`**, because an edge can carry several OSM ways and `osmid` shows only one.
- **`scripts/compare_versions.py`** compares all of Davidson County with `--areas davidson` and reports download size, first-run time, graph size and service-road use. Each version's wrong-level check uses its own graph, and pairs are compared by OSM way and XD.

## [0.3.0] - 2026-11-20

0.2.0 was never published to PyPI, so this release also carries its changes. Upgrading from 0.1.2 changes the inputs, the settings and the output, as listed below.

### Changed
- **Python 3.11 or later is required.** The package now uses osmnx 2 (`osmnx>=2.1.1,<3`), `geopandas>=1.0.1`, `pandas>=2.1`, `shapely>=2.0` and `numpy>=1.26`. fiona is no longer needed.
- **New matching rules.** An OSM edge is matched to an XD when:
  - it carries an OSM way that INRIX's conflation lists for the XD;
  - it points the XD's direction of travel;
  - it runs at least `min_overlap_m` along the XD, inside a band `band_half_width_m` either side of the XD that is cut square at the XD's ends.

  Cross streets and the next block past an XD's end are no longer matched.
- **Direction of travel** comes from the geometry of the edge and the XD where they meet. INRIX's P/N way direction is compared with it on two-way streets, and disagreements are logged as warnings.
- **Settings.** `threshold_distance` is replaced by `config=MatchConfig(...)`. See Added.
- **Output columns.**
  - `distance` and `within_threshold` are removed.
  - `overlap_m` (how far the edge runs along the XD, in meters) and `source` (`conflation` or `geometry`) are added.
  - Geometries are in EPSG:4326 (longitude, latitude) instead of EPSG:3857.
  - There is one row per edge and XD pair.
  - ID columns are integers, also when the result is empty.
- **Required input fields.** The XD GeoJSON must have `County`, `XDSegID`, `FRC` and `RoadName`, in EPSG:4326. `Bearing` is no longer read.
- **County names** are matched case-insensitively, and a single name may be passed as a string.
- **`parallel`** uses every CPU core but `reserved_cores` (2 by default), and returns the same result as `serial`.
- **Errors are raised instead of silently returning an empty table.**
  - Bad inputs raise `ValueError` before any download, naming the file, the column and the offending values.
  - A bad `config` raises `TypeError` or `ValueError` naming the setting.
  - A failure inside a county raises `RuntimeError` naming the county.
- **Logging.** The package logs through Python's `logging` module and no longer prints or silences warnings for the whole program.

### Added
- **`MatchConfig`** holds every tunable number:
  - `band_half_width_m`, 15 m by default;
  - `min_overlap_m`, 10 m;
  - `fallback_below_coverage`, 0.5;
  - `hull_buffer_m`, 200 m;
  - `reserved_cores`, 2.

  It checks every value, and `MatchConfig.from_toml(path)` loads settings from a TOML file.
- **Geometric fallback.** XDs whose INRIX way IDs no longer match OSM are searched by geometry alone. A road-level rule stops an XD from matching a road on another level, such as the road under a bridge.
- **`geo_mapper_api.evaluation`** measures a result:
  - XD coverage,
  - edges that only touch their XD,
  - matches on the wrong level at interstate crossings,
  - XDs with no OSM road nearby.
- **README** gains a quick start, a description of how a match is decided, a settings table and the known limits.

### Fixed
- The distance check measured Web Mercator units, about 20 m at Nashville's latitude, while calling them meters.
- `threshold_distance` was ignored; it was always replaced by 25.
- An edge carrying several OSM ways produced duplicate rows.
- A county where every candidate group had one row crashed, and the crash was hidden as an empty result.
- XDs with an INRIX bearing other than N, E, S or W were dropped.
- `parallel("COUNTY")` with a single string ran one job per letter.
- Edges whose OSM tags lacked `lanes`, `name` or `reversed` crashed the merge.
- Importing the package silenced every warning in the calling program.

### Removed
- `threshold_distance` from `serial` and `parallel`.
- Functions in `converters.py` that served the old matching, including `plot_matched_geometry`, `load_and_prepare_inrix_data`, `merge_and_filter_inrix_osm`, `apply_bearing_classification`, `find_overlapping_geometries`, `process_top_groups` and `check_osm_inrix_proximity`.

### Results
On downtown Nashville (1,612 XDs), compared with 0.2.0, the last version before the new rules:
- unmatched XDs fell from 164 to 31;
- edges that only touch their XD fell from 912 to 0;
- matched XDs at least 90% covered rose from 1,307 of 1,443 to 1,450 of 1,570 (counting XDs with an OSM road nearby);
- wrong-level matches at 78 interstate crossings stayed at 0;
- a run takes about 4 s instead of 30 s.

On Crockett County (394 XDs), unmatched XDs fell from 84 to 7.

## [0.1.2] - 2025-07-03

Last release before this changelog.

[0.3.1]: https://github.com/scope-lab-vu/geospatial_mapper/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/scope-lab-vu/geospatial_mapper/compare/f9bf28a...v0.3.0
[0.1.2]: https://pypi.org/project/geo-mapper-api/0.1.2/
