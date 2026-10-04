"""Measure what one History request costs a web worker, in a real library.

The production evidence this exists for is a single filtered
`GET /api/v1/history/` that grew a gunicorn worker by ~182 MiB while
returning ~17 KiB. A synthetic fixture does not reproduce that: the cost is
in hydrating fat `Item` rows, so it only appears against a real database.

Reports the serving worker's RSS/PSS/private/anonymous before the request,
immediately after, and at +30/+60/+120s, plus the request's duration, query
count, response size and a hash of the canonicalized response body, so a
candidate can be compared to a baseline on memory *and* on equivalence.

Run only inside the disposable benchmark container.
"""

# Standalone diagnostic: bootstrap Django before importing models; emit JSON.
# ruff: noqa: INP001, E402, T201, S310

import hashlib
import json
import os
import sys
import time
from importlib import import_module
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if os.environ.get("FLOPPY_MEMORY_FIXTURE") != "disposable":
    message = "Only run through the disposable memory benchmark harness"
    raise SystemExit(message)

sys.path.insert(0, "/floppy")
sys.argv = ["manage.py", "shell"]
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count

from app.models import Episode

BASE = "http://127.0.0.1:8000"
SETTLE_OFFSETS = (30, 60, 120)


def worker_pids():
    """Return the gunicorn worker pids (every gunicorn process but the master)."""
    gunicorn = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "gunicorn" not in command:
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        ppid = next(
            (int(line.split()[1]) for line in status.splitlines()
             if line.startswith("PPid:")),
            0,
        )
        gunicorn[int(entry.name)] = ppid
    return sorted(pid for pid, ppid in gunicorn.items() if ppid in gunicorn)


def worker_memory():
    """Return per-worker RSS/PSS/private/anonymous KiB, plus the totals."""
    wanted = {
        "Rss": "rss_kib",
        "Pss": "pss_kib",
        "Private_Clean": "private_clean_kib",
        "Private_Dirty": "private_dirty_kib",
        "Pss_Anon": "anon_pss_kib",
        "Pss_File": "file_pss_kib",
    }
    workers = {}
    for pid in worker_pids():
        values = {}
        try:
            rollup = Path(f"/proc/{pid}/smaps_rollup").read_text()
        except OSError:
            continue
        for line in rollup.splitlines():
            key, _, rest = line.partition(":")
            if key in wanted:
                values[wanted[key]] = int(rest.split()[0])
        values["private_kib"] = (
            values.get("private_clean_kib", 0) + values.get("private_dirty_kib", 0)
        )
        workers[pid] = values
    totals = {}
    for values in workers.values():
        for key, value in values.items():
            totals[key] = totals.get(key, 0) + value
    return {"workers": workers, "total": totals}


def cgroup_memory():
    """Return the container's cgroup current/peak and its file-backed share."""
    out = {}
    for name, key in (("memory.current", "current"), ("memory.peak", "peak")):
        try:
            out[key] = int(Path(f"/sys/fs/cgroup/{name}").read_text().strip())
        except (OSError, ValueError):
            out[key] = None
    try:
        for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines():
            field, _, value = line.partition(" ")
            if field in ("file", "anon"):
                out[field] = int(value)
    except OSError:
        pass
    return out


def auth_headers(user):
    """Return API headers for the user, plus a web session cookie.

    The API authenticates by token only, so a session cookie alone answers 403.
    """
    engine = import_module(settings.SESSION_ENGINE)
    session = engine.SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session.create()
    return {
        "Authorization": f"Bearer {user.token}",
        "Cookie": f"{settings.SESSION_COOKIE_NAME}={session.session_key}",
    }


def pick_user():
    """Return the user with the most dated episode plays."""
    user_model = get_user_model()
    counts = (
        Episode.objects.filter(end_date__isnull=False)
        .values("related_season__user")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    first = next(iter(counts), None)
    if first is None:
        message = "The seeded database has no episode history"
        raise SystemExit(message)
    return user_model.objects.get(pk=first["related_season__user"])


def fetch(route, headers):
    """Request one route, returning status, body and elapsed seconds."""
    started = time.monotonic()
    request = Request(BASE + route, headers=headers)
    try:
        with urlopen(request, timeout=900) as response:
            body = response.read()
            return response.status, body, time.monotonic() - started
    except HTTPError as error:
        return error.code, error.read(), time.monotonic() - started


def canonical_hash(body):
    """Hash the response in a form that ignores key ordering and pagination URLs."""
    try:
        payload = json.loads(body)
    except ValueError:
        return hashlib.sha256(body).hexdigest()[:16]
    if isinstance(payload, dict):
        payload.pop("next", None)
        payload.pop("previous", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode(),
    ).hexdigest()[:16]


def emit(record):
    """Write one NDJSON record."""
    print(json.dumps(record, default=str), flush=True)


def measure(label, route, headers, *, settle=True):
    """Run one request with memory readings around it."""
    before = worker_memory()
    status, body, seconds = fetch(route, headers)
    after = worker_memory()
    record = {
        "phase": "request",
        "label": label,
        "route": route,
        "status": status,
        "seconds": round(seconds, 3),
        "response_bytes": len(body),
        "response_hash": canonical_hash(body),
        "before": before,
        "after": after,
        "delta_total_rss_kib": after["total"].get("rss_kib", 0)
        - before["total"].get("rss_kib", 0),
        "delta_total_pss_kib": after["total"].get("pss_kib", 0)
        - before["total"].get("pss_kib", 0),
        "delta_total_anon_pss_kib": after["total"].get("anon_pss_kib", 0)
        - before["total"].get("anon_pss_kib", 0),
        "cgroup_after": cgroup_memory(),
        "settled": {},
        "recycled": sorted(after["workers"]) != sorted(before["workers"]),
    }
    if settle:
        elapsed = 0
        for offset in SETTLE_OFFSETS:
            time.sleep(offset - elapsed)
            elapsed = offset
            record["settled"][str(offset)] = worker_memory()["total"]
    emit(record)
    return record


def main():
    """Measure the filtered-history scenarios, then a repeated-cycle run."""
    user = pick_user()
    headers = auth_headers(user)
    episodes = Episode.objects.filter(
        related_season__user=user, end_date__isnull=False,
    ).count()
    emit({
        "phase": "start",
        "user_id": user.pk,
        "episode_plays": episodes,
        "workers": worker_pids(),
        "baseline": worker_memory(),
        "cgroup": cgroup_memory(),
    })

    scenarios = json.loads(
        os.environ.get("FLOPPY_HISTORY_SCENARIOS") or json.dumps([
            # The production shape: a filter that bypasses the day cache, over a
            # history far larger than the page it returns.
            ["filtered-tv-all", "/api/v1/history/?media_type=tv&start_date=1900-01-01"],
            ["filtered-tv-decade", "/api/v1/history/?media_type=tv&start_date=2016-01-01"],
            ["cached-type-only", "/api/v1/history/?media_type=tv"],
        ]),
    )

    # A cold worker pays one-off import costs; pay those, but do NOT pre-run the
    # scenarios. glibc keeps a grown arena, so a warm-up request would move the
    # whole cost into an unmeasured call and leave every later delta reading ~0.
    fetch("/api/v1/info/", headers)

    # A scaling ladder wants the delta per scenario, not the retention curve;
    # settling six scenarios costs six minutes of nothing happening.
    settle = os.environ.get("FLOPPY_HISTORY_SETTLE", "1") != "0"
    for run in range(int(os.environ.get("FLOPPY_HISTORY_RUNS", "2"))):
        for label, route in scenarios:
            measure(f"{label}#{run}", route, headers, settle=settle)

    # Repeated-cycle: does the worker settle back, or ratchet upward?
    cycle_route = scenarios[0][1]
    for cycle in range(int(os.environ.get("FLOPPY_HISTORY_CYCLES", "6"))):
        measure(f"cycle#{cycle}", cycle_route, headers, settle=False)
    time.sleep(60)
    emit({"phase": "after-cycles", "total": worker_memory()["total"],
          "cgroup": cgroup_memory(), "workers": worker_pids()})


main()
