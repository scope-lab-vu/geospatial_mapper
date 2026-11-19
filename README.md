# Setup
This needs Python 3.9 or 3.10, because it pins older pandas and geopandas releases. Version 0.2.0 does not support Python 3.11 or later.
```
pip install geo_mapper_api
```

# Requirements
It needs both the `OSMConflation` and `GeoJSON` data for the target area. You can obtain both from Inrix [Data Download Service](https://docs.inrix.com/datadownload/datadownload/) provided you have an access token or login credentials.

This conflation `csv` should have the following features:
| Feature                 | Type    | Example   |
| ----------------------- | ------- | --------- |
| XDSegID                 | Integer | 136894283 |
| OSMWayIDs               | String  | 19659968;19659970 |
| OSMWayDirections        | String  | N;N       |
| WayStartOffset\_m       | Float   | 1077.78   |
| WayEndOffset\_m         | Float   | 1851.31   |
| WayStartOffset\_percent | Float   | 33.706    |
| WayEndOffset\_percent   | Float   | 57.897    |

Only `XDSegID`, `OSMWayIDs` and `OSMWayDirections` are required. `OSMWayIDs` and `OSMWayDirections` hold `;`-separated lists of the same length.

The `geojson` data needs `County`, `XDSegID`, `Bearing` and a line geometry in EPSG:4326. INRIX files also carry fields such as these:
| Feature    | Type     | Example            |
| ---------- | -------- | ------------------ |
| OID        | Integer  | 7931440            |
| XDSegID    | Integer  | 156418860          |
| PreviousXD | Float    | nan                |
| NextXDSegI | Float    | 395960459.0        |
| FRC        | Integer  | 4                  |
| RoadNumber | Float    | nan                |
| RoadName   | String   | DRHESSRD           |
| LinearID   | Float    | nan                |
| Country    | String   | UNITEDSTATES       |
| State      | String   | TENNESSEE          |
| County     | String   | HAYWOOD            |
| District   | Float    | nan                |
| PostalCode | String   | 38006              |
| Miles      | Float    | 0.5902665205613952 |
| Lanes      | Float    | 1.0                |
| SlipRoad   | Integer  | 0                  |
| SpecialRoa | Float    | nan                |
| RoadList   | String   | DRHESSRD           |
| StartLat   | Float    | 35.67248           |
| StartLong  | Float    | -89.14147          |
| EndLat     | Float    | 35.666218484838986 |
| EndLong    | Float    | -89.13571015096953 |
| Bearing    | String   | S                  |
| XDGroup    | Integer  | 2013963            |
| ShapeSRID  | Integer  | 4326               |
| geometry   | Geometry | LINESTRING         |

# Usage
Give it the map release GeoJSON, the conflation CSV, and one or more county names. County names are matched case-insensitively.
```python
import os

from geo_mapper_api import inrix_to_osm

DATA_DIR = "./data"
geojson_path = os.path.join(DATA_DIR, "USA_Tennessee.geojson")
csv_path = os.path.join(DATA_DIR, "USA_Tennessee.csv")

if __name__ == "__main__":
    df = inrix_to_osm.parallel(geojson_path, csv_path, ["WILLIAMSON", "DAVIDSON"], threshold_distance=25)
    df.to_csv(os.path.join(DATA_DIR, "inrix_osm.csv"), index=False)
```
`parallel` runs each county in its own process, using every CPU core but two. It must be called under `if __name__ == "__main__":`, because the worker processes import your script again. `serial` takes the same arguments, runs one county at a time, and needs no guard.

The result has one row per OSM edge and XD segment pair:
| Column           | Type     | Example                                   |
|------------------|----------|-------------------------------------------|
| u                | int      | 202619796                                 |
| v                | int      | 202705554                                 |
| key              | int      | 0                                         |
| osmid            | int      | 19495638                                  |
| XDSegID          | int      | 1524404117                                |
| osm_geom         | geometry | LINESTRING (-86.7801 36.1602, ...)        |
| inrix_geom       | geometry | LINESTRING (-86.7800 36.1603, ...)        |
| distance         | float    | 1.42                                      |
| within_threshold | bool     | True                                      |
| county           | str      | DAVIDSON                                  |

- `u`, `v` and `key` identify the edge in the osmnx graph. `osmid` is the OSM way that links the edge to the XD. When several ways do, it is the smallest of their IDs.
- Geometries are in EPSG:4326 (longitude, latitude).
- `distance` is the closest distance in meters between the edge and the XD line. `threshold_distance` is in meters too.
- `county` holds the upper-cased county name.

# Errors
- Bad inputs raise `ValueError` before any county is processed. This covers a missing column, an unknown county, an empty county list, a non-numeric ID, and a conflation row whose way and direction lists differ in length. The message names the file, the column and the offending values.
- A failure while processing a county raises `RuntimeError` with a message like `county 'DAVIDSON' failed: ConnectionError: ...`.

# Logging
The package logs through Python's `logging` module and writes no files itself. To keep its logs:
```python
import logging
logging.basicConfig(filename="log/geo_mapper.log", level=logging.DEBUG)
```

# Development
```
python3.10 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
The unit tests run on small hand-made data and need no network.

The integration test runs on real INRIX data for downtown Nashville. The data is licensed, so it is never committed, and `tests/data/` is gitignored. Build it, then point the test at it:
```
.venv/bin/python scripts/make_test_data.py --geojson-src HOST:/path/USA_Tennessee.geojson --csv-src HOST:/path/conflation.csv
GEO_MAPPER_TEST_DATA=tests/data .venv/bin/pytest
```
The first run downloads the OSM network into `tests/data/osm_cache`. Later runs reuse it.

# Releasing
Set the new `version` in `pyproject.toml`, merge it to `main`, then push a matching tag such as `git tag v0.2.0 && git push origin v0.2.0`. The publish workflow checks that the tag matches the version and uploads to PyPI.
