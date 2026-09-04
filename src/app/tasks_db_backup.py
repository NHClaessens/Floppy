"""Periodic raw SQLite snapshot for disaster recovery (#1053).

CSV export (integrations.exports.write_backup) cannot replace a physically
damaged db.sqlite3: it needs a working Django install to import into, and it
carries only media/ratings/lists, not accounts or integration credentials.
This task writes a verified, atomically-published copy of the live database
itself, so a corrupted file has something real to be replaced with.
"""

import logging
from pathlib import Path

from celery import shared_task
from django.conf import settings

from config.sqlite_integrity import create_live_database_snapshot

logger = logging.getLogger(__name__)


@shared_task(name="Write database snapshot", ignore_result=True)
def write_database_snapshot():
    """Write a verified raw .sqlite3 snapshot to BACKUP_DIR/database/.

    No-ops when the deployment uses PostgreSQL, or when the feature is
    disabled -- checked here, not only at schedule-definition time, so
    toggling DB_SNAPSHOT_ENABLED takes effect on the next run. Never raises:
    a snapshot failure must never crash a worker or retry-storm.
    """
    if not settings.USING_SQLITE_DATABASE:
        return {"status": "skipped", "reason": "postgres"}
    if not settings.DB_SNAPSHOT_ENABLED:
        return {"status": "skipped", "reason": "disabled"}

    try:
        dest_dir = Path(settings.BACKUP_DIR) / "database"
        path = create_live_database_snapshot(
            str(settings.FLOPPY_DB_PATH),
            dest_dir,
            max_keep=settings.DB_SNAPSHOT_RETENTION_COUNT,
            timeout_seconds=settings.SQLITE_BUSY_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Database snapshot task failed unexpectedly")
        return {"status": "error", "reason": "unexpected failure"}

    if path is None:
        return {"status": "error", "reason": "snapshot could not be verified"}

    logger.info("Database snapshot written to %s", path)
    return {"status": "ok", "path": str(path)}
