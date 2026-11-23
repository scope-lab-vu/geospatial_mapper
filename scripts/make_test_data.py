"""Build the test areas from the INRIX map release files.

Writes three areas: downtown Nashville (Davidson XDs inside a box around the interstate loop),
all of Crockett County, and all of Davidson County. The integration tests use downtown and
Crockett. davidson is only for scripts/compare_versions.py --areas davidson.
The source files are licensed and must never be committed.
Pass their scp locations the first time; later runs reuse the copies in <out>/raw/.
The output folder (tests/data by default) is gitignored.

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


def area_masks(segments):
    """Each area's name and a mask selecting its XDs."""
    return {
        "downtown": (segments["County"] == "DAVIDSON") & segments.geometry.within(DOWNTOWN),
        "crockett": segments["County"] == "CROCKETT",
        "davidson": segments["County"] == "DAVIDSON",
    }


def fetch(source, raw_dir, suffix):
    """Copy one scp source into raw_dir, or reuse the single *suffix file already there."""
    if source is None:
        found = sorted(raw_dir.glob(f"*{suffix}"))
        if len(found) != 1:
            sys.exit(f"pass the {suffix} source: {raw_dir} holds {len(found)} {suffix} files, expected 1")
        logger.info("using existing %s", found[0])
        return found[0]
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
    parser.add_argument("--geojson-src", help="scp source of the INRIX map release GeoJSON (needed the first time)")
    parser.add_argument("--csv-src", help="scp source of the INRIX OSM conflation CSV (needed the first time)")
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
    geojson = fetch(args.geojson_src, raw_dir, ".geojson")
    csv = fetch(args.csv_src, raw_dir, ".csv")

    segments = gpd.read_file(geojson)
    conflation = pd.read_csv(csv, dtype={"OSMWayIDs": str, "OSMWayDirections": str})
    for area, mask in area_masks(segments).items():
        subset = segments[mask]
        if subset.empty:
            sys.exit(f"no XD segments for area {area} in {geojson}")
        rows = conflation[conflation["XDSegID"].isin(set(pd.to_numeric(subset["XDSegID"])))]
        subset.to_file(args.out / f"{area}.geojson", driver="GeoJSON")
        rows.to_csv(args.out / f"{area}.csv", index=False)
        logger.info("%s: wrote %d XD segments and %d conflation rows (covering %d XDs) to %s",
                    area, len(subset), len(rows), rows["XDSegID"].nunique(), args.out)
        print(f"{area}: {len(subset)} XD segments, {len(rows)} conflation rows written to {args.out}")


if __name__ == "__main__":
    main()
