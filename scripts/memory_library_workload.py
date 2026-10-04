"""Drive a real library's heaviest pages, to reproduce web-worker growth.

The synthetic fixture in memory_workload.py measures what the code costs. It
does not reproduce what production does to a web worker -- with recycling
disabled it stayed flat at ~102 MiB for hours, while a production worker of
the same age held 567 MiB of private memory.

The difference is the library. This drives the routes that dominate
slow_request in a real instance, against a copy of a real database, and
reports the worker's resident size beside each one so growth can be attributed
to a route rather than to elapsed time.

Run only inside the disposable benchmark container.
"""

# Standalone diagnostic: bootstrap Django before importing models; emit JSON.
# ruff: noqa: INP001, E402, T201, S310

import json
import os
import sys
import time
from http import HTTPStatus
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

from app.models import Item, Movie

BASE = "http://127.0.0.1:8000"


def worker_resident_kib():
    """Return the summed RSS of the gunicorn workers, in KiB.

    Read from inside the container rather than sampled outside it, so each
    reading lands between two known requests instead of on a timer.
    """
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            if "gunicorn" not in command:
                continue
            status = (entry / "status").read_text()
        except (OSError, UnicodeDecodeError):
            continue
        # The master is the one whose parent is not another gunicorn process.
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                total += int(line.split()[1])
    return total


def pick_user():
    """Return the user with the most tracked movies, and a session cookie."""
    user_model = get_user_model()
    user = (
        user_model.objects.annotate(tracked=Count("movie"))
        .order_by("-tracked")
        .first()
    )
    if user is None:
        message = "The seeded database has no users"
        raise SystemExit(message)
    # SESSION_ENGINE is not the default here; a hardcoded backend silently
    # creates a session nothing reads.
    engine = import_module(settings.SESSION_ENGINE)
    session = engine.SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session.create()
    return user, f"{settings.SESSION_COOKIE_NAME}={session.session_key}"


def routes_for(user):
    """Return the heavy routes, with real identifiers from this library."""
    routes = [
        "/statistics/fragments/talent?range_name=All%20Time",
        "/statistics",
        "/medialist/movie",
        "/medialist/tv",
        "/history",
        "/lists",
        "/home/rest/",
    ]
    # A details page is the single biggest query consumer in production
    # (~113 queries each), so drive real ones rather than a synthetic stub.
    for movie in Movie.objects.filter(user=user).select_related("item")[:6]:
        item = movie.item
        routes.append(
            f"/details/{item.source}/{item.media_type}/{item.media_id}",
        )
    routes.extend(
        f"/details/{item.source}/{item.media_type}/{item.media_id}"
        for item in Item.objects.filter(media_type="podcast")[:2]
    )
    return routes


def fetch(route, cookie):
    """Request one route, returning (status, bytes read, seconds)."""
    started = time.monotonic()
    request = Request(BASE + route, headers={"Cookie": cookie})
    try:
        with urlopen(request, timeout=600) as response:
            read = 0
            while chunk := response.read(65536):
                read += len(chunk)
            return response.status, read, time.monotonic() - started
    except HTTPError as error:
        return error.code, 0, time.monotonic() - started


def main():
    """Loop the heavy routes, reporting worker RSS beside each request."""
    user, cookie = pick_user()
    routes = routes_for(user)
    print(
        json.dumps(
            {
                "phase": "start",
                "at": round(time.time(), 3),
                "routes": len(routes),
                "tracked_movies": Movie.objects.filter(user=user).count(),
                "items": Item.objects.count(),
                "baseline_worker_rss_kib": worker_resident_kib(),
            },
        ),
        flush=True,
    )
    for cycle in range(int(os.environ.get("FLOPPY_LIBRARY_CYCLES", "40"))):
        for route in routes:
            before = worker_resident_kib()
            status, read, seconds = fetch(route, cookie)
            after = worker_resident_kib()
            print(
                json.dumps(
                    {
                        "phase": "http",
                        "at": round(time.time(), 3),
                        "cycle": cycle,
                        # Truncated: a details path carries a title slug.
                        "route": route.split("?")[0][:64],
                        "status": status,
                        "ok": status == HTTPStatus.OK,
                        "bytes": read,
                        "seconds": round(seconds, 3),
                        "worker_rss_kib_before": before,
                        "worker_rss_kib_after": after,
                        "worker_rss_delta_kib": after - before,
                    },
                ),
                flush=True,
            )


main()
