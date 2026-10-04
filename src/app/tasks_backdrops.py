"""Celery task that warms horizontal backdrops into Redis.

Kept out of ``app.tasks`` so the web and interactive processes that enqueue it
import only this module, not the full background task registry.
"""

from __future__ import annotations

from celery import shared_task


@shared_task(name="Warm backdrops", ignore_result=True)
def warm_backdrops_task(identities: list[dict]):
    """Fetch missing horizontal backdrops into Redis.

    Scheduled by ``backdrops.schedule_backdrop_warm`` from read paths that
    must not call providers themselves; the next read picks the result up
    from the cache.
    """
    from app import backdrops

    for identity in identities or ():
        # resolve_backdrop is best-effort and caches both hits and misses.
        backdrops.resolve_backdrop(identity, allow_network=True)
