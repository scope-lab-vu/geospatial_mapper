"""Compare geo-mapper-api output between a base commit and the working tree.

Each version runs in its own Python process, on the same data and OSM cache, so two
package versions with different dependencies can be compared side by side:

    python scripts/compare_versions.py --base f9bf28a --base-python .venv-0.2/bin/python

The base runs from its git commit with --base-python. The working tree runs with the
Python running this script. Results stay in <data>/compare/ (gitignored) and the
tables go to stdout and log/compare_versions_<time>.md.

The child part of this file also runs on the base Python, so it must stay compatible
with the oldest Python a base version supports (3.10 for 0.2.0).
"""
import argparse
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

AREAS = {"downtown": "DAVIDSON", "crockett": "CROCKETT"}
REPO = Path(__file__).resolve().parent.parent
GEOMETRY_COLUMNS = ("osm_geom", "inrix_geom")
PAIR = ["u", "v", "key", "XDSegID"]
RUNS = 2  # the second run is timed, the first may download OSM data

logger = logging.getLogger("compare_versions")


def child(args):
    """Run one version on one area and write its result and timing."""
    import osmnx as ox

    ox.settings.use_cache = True
    ox.settings.cache_folder = str(Path(args.data) / "osm_cache")

    import importlib.util

    import geo_mapper_api
    from geo_mapper_api import inrix_to_osm

    # Versions with the matching module log their P/N disagreement count; collect it here.
    has_pn = importlib.util.find_spec("geo_mapper_api.matching") is not None
    records = []
    handler = logging.Handler(logging.INFO)
    handler.emit = records.append
    matching_logger = logging.getLogger("geo_mapper_api.matching")
    matching_logger.addHandler(handler)
    matching_logger.setLevel(logging.INFO)

    data = Path(args.data)
    start = time.perf_counter()
    result = inrix_to_osm.serial(str(data / f"{args.area}.geojson"), str(data / f"{args.area}.csv"), AREAS[args.area])
    seconds = time.perf_counter() - start
    pn = sum(getattr(r, "pn_disagreements", 0) for r in records) if has_pn else None

    out = result.copy()
    for column in GEOMETRY_COLUMNS:
        out[column] = [geom.wkb_hex for geom in out[column]]
    out.to_csv(args.out_csv, index=False)
    Path(args.out_json).write_text(json.dumps({"file": geo_mapper_api.__file__, "seconds": seconds, "pn": pn}))


def extract_base(base, into):
    """Extract src/ of a git commit into a folder and return the src path."""
    archive = subprocess.run(["git", "archive", "--format=tar", base, "src"], cwd=REPO, capture_output=True, check=False)
    if archive.returncode != 0:
        sys.exit(f"git archive {base} failed: {archive.stderr.decode().strip()}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    return Path(into) / "src"


def run_version(label, python, src, area, data, out_dir):
    """Run one version RUNS times on an area; return (result frame, seconds of the last run, P/N count)."""
    import pandas as pd

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["PYTHONPATH"] = str(src)
    results = []
    for run in range(1, RUNS + 1):
        out_csv = out_dir / f"{label}_{area}.csv"
        out_json = out_dir / f"{label}_{area}.json"
        command = [str(python), str(Path(__file__).resolve()), "--child", "--area", area,
                   "--data", str(data), "--out-csv", str(out_csv), "--out-json", str(out_json)]
        logger.info("run %d of %s on %s: %s", run, label, area, " ".join(command))
        done = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            sys.exit(f"{label} on {area} failed (exit {done.returncode}):\n{done.stderr}")
        info = json.loads(out_json.read_text())
        if not Path(info["file"]).resolve().is_relative_to(Path(src).resolve()):
            sys.exit(f"{label} imported geo_mapper_api from {info['file']}, not from {src}")
        frame = pd.read_csv(out_csv)
        results.append((frame, info["seconds"], info.get("pn")))
        logger.debug("%s %s run %d: %d rows in %.1f s", label, area, run, len(frame), info["seconds"])
    first, last = (set(map(tuple, r[0][PAIR].itertuples(index=False))) for r in (results[0], results[-1]))
    if first != last:
        sys.exit(f"{label} on {area} gave different pairs on two runs ({len(first ^ last)} differ)")
    return results[-1]


def table(area, data, base_label, base, head):
    """Score both versions with the working tree's evaluation measures and format one table."""
    import osmnx as ox
    import shapely

    import geo_mapper_api.converters as gma
    from geo_mapper_api import MatchConfig, evaluation

    ox.settings.use_cache = True
    ox.settings.cache_folder = str(data / "osm_cache")
    config = MatchConfig()
    xds = gma.load_and_prepare_inrix_data_for_multiple_counties(data / f"{area}.geojson", [AREAS[area]])
    edges = evaluation.area_edges(xds, config)
    total = len(xds)

    scored = []
    for frame, _, _ in (base, head):
        frame = frame.copy()
        for column in GEOMETRY_COLUMNS:
            frame[column] = shapely.from_wkb(frame[column].map(bytes.fromhex))
        scored.append((evaluation.summary(frame, xds, edges, config),
                       set(map(tuple, frame[PAIR].itertuples(index=False)))))
    (base_m, base_pairs), (head_m, head_pairs) = scored
    rows = [(name, base_m[name], head_m[name]) for name in base_m]
    rows.append(("P/N disagreements", "n/a" if base[2] is None else base[2], "n/a" if head[2] is None else head[2]))
    rows.append(("pairs only in this version", len(base_pairs - head_pairs), len(head_pairs - base_pairs)))
    rows.append(("run seconds", f"{base[1]:.1f}", f"{head[1]:.1f}"))
    lines = [f"### {area} ({AREAS[area]}, {total} XDs)", "", f"| Measure | {base_label} | working tree |", "|---|---|---|"]
    lines += [f"| {name} | {b} | {h} |" for name, b, h in rows]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="f9bf28a", help="base commit (default f9bf28a, which is 0.2.0)")
    parser.add_argument("--base-python", help="Python with the base version's dependencies")
    parser.add_argument("--data", default="tests/data", help="folder with <area>.geojson, <area>.csv and osm_cache")
    parser.add_argument("--areas", nargs="+", default=list(AREAS), choices=list(AREAS))
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--area", help=argparse.SUPPRESS)
    parser.add_argument("--out-csv", help=argparse.SUPPRESS)
    parser.add_argument("--out-json", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    if not args.base_python:
        parser.error("--base-python is required")

    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    Path("log").mkdir(exist_ok=True)
    logging.basicConfig(filename=f"log/compare_versions_{stamp}.log", level=logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data = Path(args.data).resolve()
    out_dir = data / "compare"
    out_dir.mkdir(exist_ok=True)
    report = [f"## geo-mapper-api {args.base} vs working tree ({stamp})", ""]
    with tempfile.TemporaryDirectory() as tmp:
        base_src = extract_base(args.base, tmp)
        for area in args.areas:
            base = run_version(args.base, args.base_python, base_src, area, data, out_dir)
            head = run_version("head", sys.executable, REPO / "src", area, data, out_dir)
            report.append(table(area, data, args.base, base, head))
    text = "\n".join(report)
    Path(f"log/compare_versions_{stamp}.md").write_text(text)
    logger.info("wrote log/compare_versions_%s.md", stamp)
    print(text)


if __name__ == "__main__":
    main()
