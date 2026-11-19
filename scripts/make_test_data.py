"""Build the downtown Nashville test subset from the INRIX map release files.

The source files are licensed and must never be committed. Pass their scp locations as
arguments; the output folder (tests/data by default) is gitignored.

    python scripts/make_test_data.py --geojson-src HOST:/path/USA_Tennessee.geojson \
        --csv-src HOST:/path/inrix_osm_conflation_USA_Tennessee.csv
"""
import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

logger = logging.getLogger("make_test_data")

# Downtown Nashville: the interstate loop, its interchanges and the Cumberland River bridges.
DOWNTOWN = box(-86.81, 36.14, -86.76, 36.18)
COUNTY = "DAVIDSON"


def fetch(source, raw_dir):
    """Copy one scp source into raw_dir unless a file with that name is already there."""
    target = raw_dir / Path(source.split(":", 1)[-1]).name
    if target.exists():
        logger.info("using existing %s", target)
        return target
    logger.info("copying %s to %s", source, target)
    if subprocess.run(["scp", source, str(target)], check=False).returncode != 0:
        sys.exit(f"scp failed for {source}")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--geojson-src", required=True, help="scp source of the INRIX map release GeoJSON")
    parser.add_argument("--csv-src", required=True, help="scp source of the INRIX OSM conflation CSV")
    parser.add_argument("--out", default="tests/data", type=Path, help="output folder (default tests/data)")
    args = parser.parse_args()

    Path("log").mkdir(exist_ok=True)
    logging.basicConfig(
        filename=f"log/make_test_data_{datetime.now():%Y%m%d_%H%M%S}.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    raw_dir = args.out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    geojson = fetch(args.geojson_src, raw_dir)
    csv = fetch(args.csv_src, raw_dir)

    segments = gpd.read_file(geojson, engine="fiona")
    subset = segments[(segments["County"] == COUNTY) & segments.geometry.within(DOWNTOWN)]
    if subset.empty:
        sys.exit(f"no {COUNTY} XD segments inside {DOWNTOWN.bounds} in {geojson}")
    subset_ids = set(pd.to_numeric(subset["XDSegID"]))

    conflation = pd.read_csv(csv, dtype={"OSMWayIDs": str, "OSMWayDirections": str})
    conflation = conflation[conflation["XDSegID"].isin(subset_ids)]

    subset.to_file(args.out / "downtown.geojson", driver="GeoJSON")
    conflation.to_csv(args.out / "downtown.csv", index=False)
    logger.info("wrote %d XD segments and %d conflation rows (covering %d XDs) to %s",
                len(subset), len(conflation), conflation["XDSegID"].nunique(), args.out)
    print(f"{len(subset)} XD segments, {len(conflation)} conflation rows written to {args.out}")


if __name__ == "__main__":
    main()
