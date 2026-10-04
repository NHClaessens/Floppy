"""Deterministic workload for disposable memory-benchmark containers only.

Run via memory_workload.sh. Uses real HTTP and Celery, without live providers.
The fixture intentionally duplicates large person biographies through credits
and list memberships to expose full-model queryset materialization.
"""

# Standalone diagnostic: bootstrap Django before importing models; emit JSON logs.
# ruff: noqa: INP001, E402, T201

import csv
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from urllib.request import Request, urlopen

if os.environ.get("FLOPPY_MEMORY_FIXTURE") != "disposable":
    message = "Only run through the disposable memory benchmark harness"
    raise SystemExit(message)

sys.path.insert(0, "/floppy")
sys.argv = ["manage.py", "shell"]  # Do not enqueue AppConfig startup work.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.db import connections
from django.utils import timezone

from app.models import (
    CollectionEntry,
    CreditRoleType,
    Item,
    ItemPersonCredit,
    Movie,
    MoviePlay,
    Person,
    PersonGender,
    Sources,
    Status,
)
from app.tasks import refresh_history_cache_task, refresh_statistics_cache_task
from integrations.models import ImportRun
from integrations.tasks import import_clz
from integrations.upload_staging import stage_uploaded_file
from lists.models import CustomList, CustomListItem

MINIMUM_TALENT_RESPONSE_BYTES = 1000
# Carried into every record so a host-side sampler can align a memory reading
# with the scale and cycle that produced it.
CONTEXT = {"scale": None, "cycle": None}


def report(phase, started, **fields):
    """Emit one machine-readable completed operation."""
    for counter in ("current", "peak"):
        path = Path(f"/sys/fs/cgroup/memory.{counter}")
        if path.exists():
            fields[f"cgroup_{counter}_bytes"] = int(path.read_text())
    print(
        json.dumps(
            {
                "phase": phase,
                # Wall clock, so this joins to samples taken outside the container.
                "started_at": round(time.time() - (time.monotonic() - started), 3),
                "ended_at": round(time.time(), 3),
                "seconds": round(time.monotonic() - started, 3),
                **CONTEXT,
                **fields,
            }
        ),
        flush=True,
    )


def seed(size):
    """Create bounded batches of synthetic records in a fresh benchmark volume."""
    started = time.monotonic()
    plays_per_item = int(os.environ.get("FLOPPY_MEMORY_PLAYS_PER_ITEM", "4"))
    history_days = int(os.environ.get("FLOPPY_MEMORY_HISTORY_DAYS", "30"))
    user = get_user_model().objects.create_user(username=f"memory-{size}")
    people = Person.objects.bulk_create(
        [
            Person(
                source=Sources.MANUAL,
                source_person_id=f"memory-{size}-{n}",
                name=f"Person {n:04}",
                gender=PersonGender.MALE,
                biography="Biography " * 1000,
            )
            for n in range(100)
        ]
    )
    lists = CustomList.objects.bulk_create(
        [CustomList(owner=user, name=f"List {n:03}") for n in range(40)]
    )
    today = timezone.now()
    for start in range(0, size, 100):
        items = Item.objects.bulk_create(
            [
                Item(
                    source=Sources.MANUAL,
                    media_type="movie",
                    media_id=f"memory-{size}-{n}",
                    title=f"Movie {n:05}",
                    runtime_minutes=90,
                    image=settings.IMG_NONE,
                )
                for n in range(start, min(size, start + 100))
            ]
        )
        movies = [
            Movie(
                user=user,
                item=item,
                status=Status.COMPLETED,
                end_date=today - timedelta(days=(start + n) % history_days),
            )
            for n, item in enumerate(items)
        ]
        Movie.objects.bulk_create(movies)
        MoviePlay.objects.bulk_create(
            [
                MoviePlay(
                    movie=movie,
                    end_date=today
                    - timedelta(days=(start + n + play) % history_days),
                )
                for n, movie in enumerate(movies)
                for play in range(plays_per_item)
            ],
            batch_size=500,
        )
        ItemPersonCredit.objects.bulk_create(
            [
                ItemPersonCredit(
                    item=item,
                    person=people[(start + n + credit) % 100],
                    role_type=CreditRoleType.CAST,
                    sort_order=credit,
                )
                for n, item in enumerate(items)
                for credit in range(20)
            ],
            batch_size=500,
        )
        for custom_list in lists:
            CustomListItem.objects.bulk_create(
                [
                    CustomListItem(
                        custom_list=custom_list,
                        item=item,
                        list_item_id=start + n + 1,
                        added_by=user,
                    )
                    for n, item in enumerate(items)
                ],
                batch_size=100,
            )
    report(
        "seed",
        started,
        items=size,
        memberships=size * 40,
        credits=size * 20,
        plays=size * plays_per_item,
    )
    session = SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session.save()
    return user, f"{settings.SESSION_COOKIE_NAME}={session.session_key}"


# The four routes a run always exercises, plus the ones production spends most
# of its web time in. The wider set is opt-in because it changes what a run
# measures: these are the endpoints that dominate slow_request in a real
# instance, so a footprint measured with them is not comparable to one without.
BASE_ROUTES = (
    "/lists?sort=name",
    "/lists?sort=last_watched",
    "/statistics/fragments/talent?range_name=All%20Time",
    "/history",
)
PRODUCTION_HEAVY_ROUTES = (
    "/medialist/movie",
    "/statistics",
    "/home/rest/",
)


def routes():
    """Return the route set this run exercises."""
    if os.environ.get("FLOPPY_MEMORY_HEAVY_ROUTES") == "1":
        return BASE_ROUTES + PRODUCTION_HEAVY_ROUTES
    return BASE_ROUTES


def browse(cookie, cycles=1, *, require_talent=True):
    """Exercise actual authenticated Gunicorn requests."""
    for cycle in range(cycles):
        for route in routes():
            started = time.monotonic()
            request = Request(
                "http://127.0.0.1:8000" + route, headers={"Cookie": cookie}
            )
            with urlopen(request, timeout=180) as response:  # noqa: S310 - fixed loopback HTTP
                if response.status != HTTPStatus.OK or "/login" in response.url:
                    message = f"Unauthenticated/failed response: {response.url}"
                    raise RuntimeError(message)
                count = 0
                while chunk := response.read(65536):
                    count += len(chunk)
            if (
                require_talent
                and "talent" in route
                and count < MINIMUM_TALENT_RESPONSE_BYTES
            ):
                message = f"Talent workload returned only {count} bytes"
                raise RuntimeError(message)
            report("http", started, route=route, cycle=cycle, response_bytes=count)


def await_task(task, phase, started):
    """Wait for real worker completion, propagating task failures."""
    # poll uses a real backend; no eager tasks or mocked execution.
    task.get(timeout=600, interval=1)
    report(phase, started, task_status=task.status)


def cycle_once(user, cookie, size, cycle):
    """Run one pass of the task and request classes against a seeded user."""
    request_workers = max(1, int(os.environ.get("FLOPPY_MEMORY_REQUEST_WORKERS", "1")))
    started = time.monotonic()
    task = refresh_statistics_cache_task.delay(user.pk, "All Time")
    with ThreadPoolExecutor(max_workers=request_workers) as pool:
        traffic = [
            pool.submit(browse, cookie, 1, require_talent=False)
            for _ in range(request_workers)
        ]
        await_task(task, "statistics_rebuild_plus_browsing", started)
        for request in traffic:
            request.result()
    browse(cookie, cycles=2)
    with tempfile.TemporaryFile(mode="w+b") as upload:
        import io

        text = io.TextIOWrapper(upload, encoding="utf-8", write_through=True)
        writer = csv.writer(text)
        writer.writerow(["Title", "Platform", "CLZ ID", "Notes", "Collection Status"])
        for index in range(size):
            writer.writerow(
                [
                    f"Memory game {size}-{cycle}-{index}",
                    "PC",
                    f"mem-{size}-{cycle}-{index}",
                    "Notes " * 100,
                    "In Collection",
                ]
            )
        text.flush()
        upload.seek(0)
        staged = stage_uploaded_file(upload)
        text.detach()
    connections.close_all()
    before = CollectionEntry.objects.filter(user=user).count()
    started = time.monotonic()
    task = import_clz.delay(str(staged), user.pk, "new", media_type="game")
    with ThreadPoolExecutor(max_workers=request_workers) as pool:
        traffic = [pool.submit(browse, cookie, 2) for _ in range(request_workers)]
        await_task(task, "import_plus_browsing", started)
        for request in traffic:
            request.result()
    connections.close_all()
    imported = CollectionEntry.objects.filter(user=user).count() - before
    if (
        imported != size
        or not ImportRun.objects.filter(
            user=user,
            status=ImportRun.Status.COMPLETED,
        ).exists()
    ):
        message = f"Incomplete import: expected {size}, got {imported}"
        raise RuntimeError(message)
    if Path(staged).exists():
        message = "Completed import left its staged upload behind"
        raise RuntimeError(message)
    report("import_verified", started, rows=imported)
    started = time.monotonic()
    task = refresh_history_cache_task.delay(user.pk, warm_days=30)
    with ThreadPoolExecutor(max_workers=request_workers) as pool:
        traffic = [pool.submit(browse, cookie, 2) for _ in range(request_workers)]
        await_task(task, "rebuild_plus_browsing", started)
        for request in traffic:
            request.result()
    browse(cookie, cycles=2)
    report("complete", started, items=size)


def exercise(size):
    """Seed one scale, then repeat the workload classes against it.

    Seeding runs once so repeated cycles age the processes through real task
    and request work rather than through an ever-growing fixture.
    """
    user, cookie = seed(size)
    for cycle in range(int(os.environ.get("FLOPPY_MEMORY_CYCLES", "1"))):
        CONTEXT["cycle"] = cycle
        cycle_once(user, cookie, size, cycle)


for scale in os.environ.get("FLOPPY_MEMORY_SCALES", "500,2000").split(","):
    CONTEXT["scale"] = int(scale)
    exercise(int(scale))
