import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock, skipUnless

from django.test import SimpleTestCase, override_settings

from app.tasks_db_backup import write_database_snapshot

# The underlying snapshot writer stages through /proc/self/fd/<fd>/<name> to
# avoid a TOCTOU race -- Linux-only, same as config.sqlite_integrity's other
# backup paths. Skip the one test that exercises the real writer on other
# platforms so a native macOS/BSD run doesn't report an unrelated failure.
requires_proc_fd_backup = skipUnless(
    sys.platform.startswith("linux"),
    "verified-snapshot path requires /proc/self/fd (Linux-only)",
)


class WriteDatabaseSnapshotTaskTests(SimpleTestCase):
    @override_settings(USING_SQLITE_DATABASE=True, DB_SNAPSHOT_ENABLED=False)
    def test_skips_when_disabled(self):
        with mock.patch("app.tasks_db_backup.create_live_database_snapshot") as snapshot:
            result = write_database_snapshot()

        snapshot.assert_not_called()
        self.assertEqual(result, {"status": "skipped", "reason": "disabled"})

    @override_settings(USING_SQLITE_DATABASE=False, DB_SNAPSHOT_ENABLED=True)
    def test_skips_on_postgres(self):
        with mock.patch("app.tasks_db_backup.create_live_database_snapshot") as snapshot:
            result = write_database_snapshot()

        snapshot.assert_not_called()
        self.assertEqual(result, {"status": "skipped", "reason": "postgres"})

    @override_settings(USING_SQLITE_DATABASE=True, DB_SNAPSHOT_ENABLED=True)
    def test_reports_error_without_raising_when_snapshot_fails(self):
        with mock.patch(
            "app.tasks_db_backup.create_live_database_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            result = write_database_snapshot()

        self.assertEqual(result["status"], "error")

    @override_settings(USING_SQLITE_DATABASE=True, DB_SNAPSHOT_ENABLED=True)
    def test_reports_error_when_snapshot_returns_none(self):
        with mock.patch(
            "app.tasks_db_backup.create_live_database_snapshot",
            return_value=None,
        ):
            result = write_database_snapshot()

        self.assertEqual(result["status"], "error")

    @requires_proc_fd_backup
    def test_happy_path_writes_a_real_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "db.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
            backup_dir = Path(tmp_dir) / "backups"

            with override_settings(
                USING_SQLITE_DATABASE=True,
                DB_SNAPSHOT_ENABLED=True,
                FLOPPY_DB_PATH=db_path,
                BACKUP_DIR=str(backup_dir),
                DB_SNAPSHOT_RETENTION_COUNT=7,
                SQLITE_BUSY_TIMEOUT_SECONDS=5,
            ):
                result = write_database_snapshot()

            self.assertEqual(result["status"], "ok")
            snapshot_files = list((backup_dir / "database").glob("*.sqlite3"))
            self.assertEqual(len(snapshot_files), 1)
