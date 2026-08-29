"""Tests for process-role detection used to partition API rate limiters.

Background workers get their own, smaller global bucket so backfills and
imports can never exhaust the per-second budget web requests and the
interactive worker rely on.
"""

from unittest.mock import patch

from django.test import SimpleTestCase
from pyrate_limiter import RedisBucket

from app.providers import services


class ProcessRoleDetectionTests(SimpleTestCase):
    """get_process_role resolves the env label with a safe fallback."""

    def test_explicit_roles_from_environment(self):
        """The supervisord-provided label wins."""
        for role in ("web", "interactive", "background", "combined"):
            with patch.dict("os.environ", {"FLOPPY_PROCESS_ROLE": role}):
                self.assertEqual(services.get_process_role(), role)

    def test_unknown_label_falls_back_to_argv_heuristic(self):
        """An unrecognized label is ignored in favor of the argv check."""
        with (
            patch.dict("os.environ", {"FLOPPY_PROCESS_ROLE": "bogus"}),
            patch.object(services.sys, "argv", ["/usr/local/bin/celery"]),
        ):
            self.assertEqual(services.get_process_role(), "background")

    def test_unlabeled_celery_process_is_background(self):
        """Celery processes without the env var can't starve the web budget."""
        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(services.sys, "argv", ["celery", "worker"]),
        ):
            services.os.environ.pop("FLOPPY_PROCESS_ROLE", None)
            self.assertEqual(services.get_process_role(), "background")

    def test_unlabeled_non_celery_process_is_web(self):
        """Gunicorn and manage.py default to the web budget."""
        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(services.sys, "argv", ["gunicorn"]),
        ):
            services.os.environ.pop("FLOPPY_PROCESS_ROLE", None)
            self.assertEqual(services.get_process_role(), "web")

    def test_background_role_uses_separate_smaller_bucket(self):
        """The module-level bucket split keys off the background role."""
        # The session is built at import time; assert the wiring constants so a
        # refactor can't silently merge the buckets back together.
        if services.PROCESS_ROLE == "background":
            self.assertTrue(services.bucket_key.endswith("_background"))
        else:
            self.assertFalse(services.bucket_key.endswith("_background"))


class PerHostRateLimitBucketTests(SimpleTestCase):
    """Per-host provider budgets (#1025) must be shared across processes.

    Each per-host LimiterAdapter used to default to an in-memory bucket
    private to the process that mounted it, so every gunicorn worker and
    Celery process got its own separate budget for the same provider host -
    the real aggregate request rate became (process count) x the configured
    limit, easily enough to trip a provider's real server-side rate limit.
    """

    def test_hardcover_adapter_uses_a_redis_backed_bucket(self):
        """Hardcover's per-minute budget is enforced via a shared bucket."""
        adapter = services.session.get_adapter(
            "https://api.hardcover.app/v1/graphql",
        )
        self.assertIs(adapter.limiter._bkclass, RedisBucket)

    def test_host_bucket_is_not_partitioned_by_process_role(self):
        """Unlike bucket_key, the host bucket name ignores process role.

        The limit represents an external provider's real ceiling, not an
        internal fairness split, so web/interactive/background processes
        must all draw from the same counter for a given host.
        """
        self.assertNotIn("_background", services._host_limiter_bucket_name)
        self.assertNotIn("_combined", services._host_limiter_bucket_name)

    def test_all_mounted_host_adapters_share_one_bucket_name(self):
        """Every per-host mount uses the same shared bucket namespace."""
        hosts = [
            "https://api.myanimelist.net/v2",
            "https://graphql.anilist.co",
            "https://api.igdb.com/v4",
            "https://api4.thetvdb.com",
            "https://comicvine.gamespot.com/api",
            "https://openlibrary.org",
            "https://api.hardcover.app/v1/graphql",
            "https://boardgamegeek.com/xmlapi2",
            "https://xbl.io/api",
        ]
        for host in hosts:
            adapter = services.session.get_adapter(host)
            self.assertIs(adapter.limiter._bkclass, RedisBucket)
            self.assertEqual(
                adapter.limiter._bucket_args.get("bucket_name"),
                services._host_limiter_bucket_name,
            )
