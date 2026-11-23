"""Compare geo-mapper-api output between a base commit and the working tree.

Each version runs in its own Python process, on the same data and OSM cache, so two
package versions with different dependencies can be compared side by side:

    python scripts/compare_versions.py --base v0.3.0 --base-python .venv/bin/python
    python scripts/compare_versions.py --base v0.3.0 --base-python .venv/bin/python --areas davidson

The base must be 0.3.0 or later. Each version's wrong-level check uses its own OSM graph,
built with its own road filter; which XDs have a road nearby is judged on the working tree's
road graph for both. Pairs are compared as (osmid, XDSegID), because the two graphs can split
roads into different edges. The same road can still show a different osmid when the graphs
merge ways differently, so a few pair differences are not real match changes.

Download size and first-run time come from the first run and are only meaningful with an
empty <data>/osm_cache. The Overpass timeout here (REQUESTS_TIMEOUT) is part of osmnx's cache
key, so a cache filled by the integration tests is not reused and the first run downloads.

The base runs from its git commit with --base-python. The working tree runs with the
Python running this script. Results stay in <data>/compare/ (gitignored) and the
tables go to stdout and log/compare_versions_<time>.md.

The child part of this file also runs on the base Python, so it must stay compatible
with the oldest Python a base version supports (3.11 for 0.3.0).
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

AREAS = {"downtown": "DAVIDSON", "crockett": "CROCKETT", "davidson": "DAVIDSON"}
REPO = Path(__file__).resolve().parent.parent
GEOMETRY_COLUMNS = ("osm_geom", "inrix_geom")
PAIR = ["u", "v", "key", "XDSegID"]
RUNS = 2  # the second run is timed, the first may download OSM data
REQUESTS_TIMEOUT = 900  # seconds; a whole-county Overpass response needs more than osmnx's 180

logger = logging.getLogger("compare_versions")


def child(args):
    """Run one version on one area and write its result and timing."""
    import osmnx as ox

    ox.settings.use_cache = True
    ox.settings.cache_folder = str(Path(args.data) / "osm_cache")
    ox.settings.requests_timeout = REQUESTS_TIMEOUT

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
    rejections = {}
    for record in records:
        for reason, count in getattr(record, "fallback_rejections", {}).items():
            rejections[reason] = rejections.get(reason, 0) + count

    out = result.copy()
    for column in GEOMETRY_COLUMNS:
        out[column] = [geom.wkb_hex for geom in out[column]]
    out.to_csv(args.out_csv, index=False)
    Path(args.out_json).write_text(json.dumps({"file": geo_mapper_api.__file__, "seconds": seconds, "pn": pn,
                                                "rejections": rejections if has_pn else None}))


def extract_base(base, into):
    """Extract src/ of a git commit into a folder and return the src path."""
    archive = subprocess.run(["git", "archive", "--format=tar", base, "src"], cwd=REPO, capture_output=True, check=False)
    if archive.returncode != 0:
        sys.exit(f"git archive {base} failed: {archive.stderr.decode().strip()}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    return Path(into) / "src"


def _cache_bytes(folder):
    """Total size of the files in an osmnx cache folder (0 if it doesn't exist yet)."""
    return sum(f.stat().st_size for f in folder.iterdir() if f.is_file()) if folder.is_dir() else 0


def run_version(label, python, src, area, data, out_dir):
    """Run one version RUNS times on an area.

    Returns a dict: frame, seconds, pn and rejections from the last (cached) run, and
    first_seconds and download_mb (OSM data the first run added to the cache).
    """
    import pandas as pd

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["PYTHONPATH"] = str(src)
    cache = Path(data) / "osm_cache"
    results = []
    for run in range(1, RUNS + 1):
        out_csv = out_dir / f"{label}_{area}.csv"
        out_json = out_dir / f"{label}_{area}.json"
        command = [str(python), str(Path(__file__).resolve()), "--child", "--area", area,
                   "--data", str(data), "--out-csv", str(out_csv), "--out-json", str(out_json)]
        logger.info("run %d of %s on %s: %s", run, label, area, " ".join(command))
        before = _cache_bytes(cache)
        done = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            sys.exit(f"{label} on {area} failed (exit {done.returncode}):\n{done.stderr}")
        info = json.loads(out_json.read_text())
        if not Path(info["file"]).resolve().is_relative_to(Path(src).resolve()):
            sys.exit(f"{label} imported geo_mapper_api from {info['file']}, not from {src}")
        frame = pd.read_csv(out_csv)
        results.append({"frame": frame, "seconds": info["seconds"], "pn": info.get("pn"),
                        "rejections": info.get("rejections"), "added_mb": (_cache_bytes(cache) - before) / 1e6})
        logger.debug("%s %s run %d: %d rows in %.1f s, %.1f MB added to the cache",
                     label, area, run, len(frame), info["seconds"], results[-1]["added_mb"])
    first, last = (set(map(tuple, r["frame"][PAIR].itertuples(index=False))) for r in (results[0], results[-1]))
    if first != last:
        sys.exit(f"{label} on {area} gave different pairs on two runs ({len(first ^ last)} differ)")
    return dict(results[-1], first_seconds=results[0]["seconds"], download_mb=results[0]["added_mb"])


def _service_only(frame, edges):
    """For each result row, whether its edge in `edges` is a service road and nothing else."""
    from geo_mapper_api import matching

    service = {k: matching._tags(h) == {"service"} for k, h in edges["highway"].items()}
    return [service.get(k, False) for k in zip(frame["u"], frame["v"], frame["key"])]


def _ways(osmids):
    """The set of OSM way IDs in a column of ints or lists of ints."""
    return {int(w) for o in osmids for w in (o if isinstance(o, list) else [o])}


def table(area, data, base_label, base, head, base_src):
    """Score both versions, each against its own OSM graph, and format one table."""
    import runpy

    import osmnx as ox
    import shapely

    import geo_mapper_api.converters as gma
    from geo_mapper_api import MatchConfig, evaluation, filters, matching

    ox.settings.use_cache = True
    ox.settings.cache_folder = str(data / "osm_cache")
    ox.settings.requests_timeout = REQUESTS_TIMEOUT
    config = MatchConfig()
    xds = gma.load_and_prepare_inrix_data_for_multiple_counties(data / f"{area}.geojson", [AREAS[area]])
    polygon = gma.get_exterior_polygon(xds, config.hull_buffer_m)
    base_filter = runpy.run_path(str(Path(base_src) / "geo_mapper_api" / "filters.py"))["base_filter"]
    logger.info("building base graph for %s", area)
    base_edges = ox.graph_to_gdfs(gma.get_osm_graph(polygon, base_filter), nodes=False)
    head_edges = ox.graph_to_gdfs(gma.get_osm_graph(polygon, filters.base_filter), nodes=False)
    conflation_ways = set(gma.load_conflation(data / f"{area}.csv", set(xds["XDSegID"]))["OSMWayIDs"])
    total = len(xds)

    scored = []
    for run, edges in ((base, base_edges), (head, head_edges)):
        frame = run["frame"].copy()
        for column in GEOMETRY_COLUMNS:
            frame[column] = shapely.from_wkb(frame[column].map(bytes.fromhex))
        measures = evaluation.summary(frame, xds, head_edges, config, highway=edges["highway"])
        service = frame.assign(service=_service_only(frame, edges))
        only_service = service.groupby("XDSegID")["service"].all()
        service_edges = sum(matching._tags(h) == {"service"} for h in edges["highway"])
        measures.update({
            "OSM edges in the area graph": len(edges),
            "service-only edges in the graph": f"{service_edges} ({service_edges / len(edges):.0%})",
            "rows on service-only edges": int(service["service"].sum()),
            "XDs matched only through service-only edges": int(only_service.sum()),
            "conflation way IDs present in the graph":
                f"{len(conflation_ways & _ways(edges['osmid']))} of {len(conflation_ways)}",
        })
        scored.append((measures, set(zip(frame["osmid"], frame["XDSegID"]))))
    (base_m, base_pairs), (head_m, head_pairs) = scored
    rows = [(name, base_m[name], head_m[name]) for name in base_m]
    rows.append(("P/N disagreements", "n/a" if base["pn"] is None else base["pn"],
                 "n/a" if head["pn"] is None else head["pn"]))
    for reason in sorted(set(base["rejections"] or {}) | set(head["rejections"] or {})):
        rows.append((f"fallback candidates rejected: {reason}",
                     "n/a" if base["rejections"] is None else base["rejections"].get(reason, 0),
                     "n/a" if head["rejections"] is None else head["rejections"].get(reason, 0)))
    rows.append(("(way, XD) pairs only in this version", len(base_pairs - head_pairs), len(head_pairs - base_pairs)))

    def mb(run):
        return f"{run['download_mb']:.1f}" if run["download_mb"] > 0 else "0 (already cached)"

    rows.append(("OSM MB downloaded (first run)", mb(base), mb(head)))
    rows.append(("first run seconds (includes download)", f"{base['first_seconds']:.1f}", f"{head['first_seconds']:.1f}"))
    rows.append(("run seconds (cached)", f"{base['seconds']:.1f}", f"{head['seconds']:.1f}"))
    lines = [f"### {area} ({AREAS[area]}, {total} XDs)", "", f"| Measure | {base_label} | working tree |", "|---|---|---|"]
    lines += [f"| {name} | {b} | {h} |" for name, b, h in rows]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="v0.3.0", help="base commit or tag, 0.3.0 or later (default v0.3.0)")
    parser.add_argument("--base-python", help="Python with the base version's dependencies")
    parser.add_argument("--data", default="tests/data", help="folder with <area>.geojson, <area>.csv and osm_cache")
    parser.add_argument("--areas", nargs="+", default=["downtown", "crockett"], choices=list(AREAS),
                        help="areas to compare (default downtown crockett; davidson is the whole county)")
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
        if not (base_src / "geo_mapper_api" / "matching.py").is_file():
            sys.exit(f"compare_versions needs a base of 0.3.0 or later: {args.base} has no geo_mapper_api/matching.py")
        for area in args.areas:
            base = run_version(args.base, args.base_python, base_src, area, data, out_dir)
            head = run_version("head", sys.executable, REPO / "src", area, data, out_dir)
            report.append(table(area, data, args.base, base, head, base_src))
    text = "\n".join(report)
    Path(f"log/compare_versions_{stamp}.md").write_text(text)
    logger.info("wrote log/compare_versions_%s.md", stamp)
    print(text)


if __name__ == "__main__":
    main()
