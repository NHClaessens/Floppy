"""Report whether aged processes return to a floor or stair-step upward.

Reads the output of scripts/memory_aging.sh. The question this answers is not
"how much memory does Floppy use" but "does a process that has done hours of
work end where it started", so everything here is reported against process age
and completed-cycle count rather than as a single number.
"""
# Standalone diagnostic run from a checkout, not an installed package.
# ruff: noqa: INP001, T201, PLR2004

import argparse
import json
import statistics
from pathlib import Path

MIB = 1024 * 1024
KIB_PER_MIB = 1024
# A role whose members are not interchangeable (a worker run with --beat parents
# both a pool child and the embedded scheduler, indistinguishable from outside).
# Its per-process series are reported individually.
SPLIT_ROLES = {"celery-worker-beat-child"}
# The workload driver runs inside the container so its requests are local, but
# it is the harness, not the application. Counting it would put its own ~100 MiB
# into every Floppy budget reported here.
HARNESS_ROLES = {"other"}
# Discarded before measuring drift: a process climbs steeply while its imports,
# connection pools and caches fill, and that is not aging.
WARMUP_SECONDS = 600
# A drift rate needs a baseline and a later reading far enough apart to mean
# something. A pool child that is retired after two minutes never has one, and
# extrapolating its warm-up to an hourly rate invents a trend it cannot show.
MIN_DRIFT_SPAN_HOURS = 0.25


def load(directory):
    """Return samples ordered by wall clock, skipping any truncated file."""
    samples = []
    for path in sorted((directory / "samples").glob("*.json")):
        try:
            sample = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        stem = path.stem.split("-")[0]
        if not stem.isdigit():
            continue
        sample["at"] = int(stem)
        samples.append(sample)
    samples.sort(key=lambda item: item["at"])
    return samples


def cycles(directory):
    """Return the completed workload cycles, oldest first."""
    path = directory / "workload.ndjson"
    if not path.exists():
        return []
    done = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("phase") == "complete":
            done.append(record)
    return done


def series(samples):
    """Return one PSS series per role, splitting roles that mix process kinds."""
    tracks = {}
    for sample in samples:
        for role, budget in sample["roles"].items():
            if role in SPLIT_ROLES:
                continue
            tracks.setdefault(role, []).append((sample["at"], budget.get("pss_kib")))
        for process in sample["processes"]:
            if process["role"] not in SPLIT_ROLES:
                continue
            # Keyed by pid so a recycled child starts a new series rather than
            # being averaged into the one it replaced.
            key = f"{process['role']}#pid{process['pid']}"
            tracks.setdefault(key, []).append((sample["at"], process.get("pss_kib")))
    return tracks


def trend(points, warmup_seconds=WARMUP_SECONDS):
    """Return the shape of one process group's footprint over the run.

    Every process climbs steeply while its imports and caches fill; reading a
    slope through that start reports warm-up as if it were aging. The drift
    here is therefore measured between the first and last quarters of what
    remains after the warm-up window, and the whole window is reported so a
    reader can see how much was discarded.
    """
    measured = [(at, value / KIB_PER_MIB) for at, value in points if value is not None]
    if len(measured) < 2:
        return None
    start = measured[0][0]
    warm = [item for item in measured if item[0] - start >= warmup_seconds] or measured
    span_hours = (warm[-1][0] - warm[0][0]) / 3600
    quarter = max(1, len(warm) // 4)
    early = statistics.median(value for _, value in warm[:quarter])
    late = statistics.median(value for _, value in warm[-quarter:])
    drift = (
        (late - early) / span_hours if span_hours >= MIN_DRIFT_SPAN_HOURS else None
    )
    return {
        "samples": len(measured),
        "warm_samples": len(warm),
        "span_hours": round(span_hours, 2),
        "first_mib": round(measured[0][1], 1),
        "last_mib": round(measured[-1][1], 1),
        "peak_mib": round(max(value for _, value in measured), 1),
        "median_mib": round(statistics.median(value for _, value in warm), 1),
        "early_mib": round(early, 1),
        "late_mib": round(late, 1),
        "drift_mib_per_hour": round(drift, 1) if drift is not None else None,
    }


def main():
    """Print the aging verdict for one run directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()

    samples = load(arguments.directory)
    if not samples:
        message = "No samples found"
        raise SystemExit(message)
    done = cycles(arguments.directory)
    span = (samples[-1]["at"] - samples[0]["at"]) / 3600

    print(f"samples={len(samples)} span={span:.2f}h cycles_completed={len(done)}")
    first, last = samples[0], samples[-1]
    for label, sample in (("first", first), ("last", last)):
        harness = sum(
            budget.get("pss_kib") or 0
            for role, budget in sample["roles"].items()
            if role in HARNESS_ROLES
        )
        application = sample["reconciliation"]["process_pss_bytes"] - harness * 1024
        print(
            f"  {label:5s} cgroup={sample['current_bytes'] / MIB:7.0f}MiB "
            f"peak={(sample['peak_bytes'] or 0) / MIB:7.0f}MiB "
            f"floppy_pss={application / MIB:7.0f}MiB "
            f"harness_pss={harness / KIB_PER_MIB:6.0f}MiB "
            f"oom_kill={sample['oom_kill']}"
        )

    print(
        f"\nper-process-group PSS, MiB "
        f"(drift measured after a {WARMUP_SECONDS}s warm-up window):"
    )
    header = (
        f"{'track':<34}{'n':>4}{'warm':>5}{'peak':>8}"
        f"{'early':>8}{'late':>8}{'drift/h':>9}"
    )
    print(header)
    rows = []
    for track, points in series(samples).items():
        summary = trend(points)
        if summary:
            rows.append((track, summary))
    for track, summary in sorted(rows, key=lambda row: -row[1]["peak_mib"]):
        print(
            f"{track:<34}{summary['samples']:>4}{summary['warm_samples']:>5}"
            f"{summary['peak_mib']:>8}{summary['early_mib']:>8}"
            f"{summary['late_mib']:>8}"
            f"{(summary['drift_mib_per_hour'] if summary['drift_mib_per_hour'] is not None else 'short'):>9}"
        )

    if done:
        print("\ncycle duration drift (a rising cost confounds a rising footprint):")
        chunk = max(1, len(done) // 6)
        for start in range(0, len(done), chunk):
            window = done[start : start + chunk]
            seconds = statistics.median(record["seconds"] for record in window)
            current = statistics.median(
                record.get("cgroup_current_bytes", 0) for record in window
            )
            print(
                f"  cycles {start:>5}-{start + len(window) - 1:<5} "
                f"median_seconds={seconds:8.2f} "
                f"median_cgroup={current / MIB:7.0f}MiB"
            )


if __name__ == "__main__":
    main()
