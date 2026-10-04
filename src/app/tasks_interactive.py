"""Tasks consumed by the dedicated interactive Celery worker."""

from celery import shared_task


@shared_task(name="Resolve live playback image")
def resolve_playback_image(user_id: int):
    """Resolve artwork for a cached live playback state in the background."""
    from app import live_playback

    live_playback.resolve_state_image(user_id)


@shared_task(name="app.tasks.statistics_sync_task")
def statistics_sync_task(user_id: int):
    """Rebuild a user's dirty Statistics days and stale ranges, within a budget.

    Runs out of time by queueing its own follow-up; if that message is ever
    lost, ``reconcile_statistics_sync_task`` finds the unfinished work.
    """
    from app import statistics_sync

    statistics_sync.sync_task_body(user_id)


@shared_task(name="Refresh statistics talent fragment", ignore_result=True)
def refresh_statistics_talent_fragment_task(
    user_id: int,
    range_name=None,
    start_date_str=None,
    end_date_str=None,
    compare_mode_param=None,
):
    """Rebuild a Statistics talent section the viewer is already seeing a copy of."""
    from django.contrib.auth import get_user_model

    from app.statistics_views import build_talent_fragment

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return
    build_talent_fragment(
        user, range_name, start_date_str, end_date_str, compare_mode_param
    )


@shared_task(name="Reconcile statistics sync", ignore_result=True)
def reconcile_statistics_sync_task():
    """Queue a sync for every user whose Statistics trail their changes.

    Lives on the interactive worker so a long import on the background worker
    cannot hold up recovery.
    """
    from app import statistics_sync

    statistics_sync.reconcile()


# Retired names from the chunked refresh run. Kept registered for one release
# so messages queued by the previous version drain into a sync instead of
# failing as unregistered tasks.
@shared_task(name="app.tasks.refresh_statistics_cache_task")
def refresh_statistics_cache_task(user_id: int, range_name: str = "", force=False):
    """Retired: queue a Statistics sync."""
    from app import statistics_sync

    statistics_sync.sync_task_body(user_id)


@shared_task(name="app.tasks.continue_statistics_refresh_task")
def continue_statistics_refresh_task(user_id: int, range_name="", run_id=""):
    """Retired: queue a Statistics sync."""
    from app import statistics_sync

    statistics_sync.sync_task_body(user_id)
