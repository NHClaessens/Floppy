from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from app.models import (
    TV,
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Sources,
    Status,
)
from app.services.tv_provider_migration import TvMigrationResult
from app.tasks_tv_provider_migration import (
    _migration_candidates_queryset,
    migrate_tv_shows_to_preferred_provider_task,
)


class MigrateTvShowsToPreferredProviderTaskTests(TestCase):
    """Tests for the nightly TVDB provider-migration batch task."""

    def setUp(self):
        """Create a TVDB-preferring user and a TMDB-preferring user."""
        cache.clear()
        self.tvdb_user = get_user_model().objects.create_user(
            username="tvdb-pref",
            password="pw12345",
        )
        self.tvdb_user.tv_metadata_source_default = Sources.TVDB.value
        self.tvdb_user.save()

        self.tmdb_user = get_user_model().objects.create_user(
            username="tmdb-pref",
            password="pw12345",
        )

    def _create_tmdb_show(self, user, media_id, *, anime=False):
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value if anime else MediaTypes.TV.value,
            title=f"Show {media_id}",
            image="",
            provider_external_ids={"tvdb_id": f"tvdb-{media_id}"},
        )
        TV.objects.create(item=item, user=user, status=Status.IN_PROGRESS.value)
        return item

    def test_skips_entirely_when_tvdb_not_configured(self):
        """No candidates should be queried or migrated when TVDB is unconfigured."""
        with patch("app.providers.tvdb.enabled", return_value=False):
            result = migrate_tv_shows_to_preferred_provider_task()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "tvdb_not_configured")

    @patch("app.services.tv_provider_migration.migrate_tv_item_to_tvdb")
    def test_only_considers_shows_tracked_by_a_tvdb_preferring_user(
        self,
        mock_migrate,
    ):
        """A show only tracked by a TMDB-preferring user should be left alone."""
        mock_migrate.return_value = TvMigrationResult(migrated=True)

        tvdb_pref_show = self._create_tmdb_show(self.tvdb_user, "111")
        self._create_tmdb_show(self.tmdb_user, "222")

        with patch("app.providers.tvdb.enabled", return_value=True):
            result = migrate_tv_shows_to_preferred_provider_task()

        self.assertEqual(result["migrated"], 1)
        mock_migrate.assert_called_once()
        (called_item,) = mock_migrate.call_args.args
        self.assertEqual(called_item.pk, tvdb_pref_show.pk)

    @patch("app.services.tv_provider_migration.migrate_tv_item_to_tvdb")
    def test_excludes_grouped_anime_items(self, mock_migrate):
        """Grouped-anime items use a separate TVDB pathway and must not be touched."""
        self._create_tmdb_show(self.tvdb_user, "333", anime=True)

        with patch("app.providers.tvdb.enabled", return_value=True):
            result = migrate_tv_shows_to_preferred_provider_task()

        mock_migrate.assert_not_called()
        self.assertEqual(result["migrated"], 0)

    def test_survives_a_per_item_crash(self):
        """A crash on one show must not stop the rest of the batch from running."""
        self._create_tmdb_show(self.tvdb_user, "444")

        with (
            patch("app.providers.tvdb.enabled", return_value=True),
            patch(
                "app.services.tv_provider_migration.migrate_tv_item_to_tvdb",
                side_effect=Exception("boom"),
            ),
        ):
            result = migrate_tv_shows_to_preferred_provider_task()

        self.assertEqual(result["errored"], 1)
        self.assertEqual(result["migrated"], 0)


class MigrationCandidateChurnTests(MigrateTvShowsToPreferredProviderTaskTests):
    """A show that cannot migrate today must not be re-asked tomorrow.

    It is not pinned - TMDB may publish the external id later - but retrying
    it every night both burned provider calls forever and, because the batch
    is taken in id order, let a backlog of unresolvable shows fill the batch
    so newly tracked shows never got a turn.
    """

    @patch("app.providers.tvdb.enabled", return_value=True)
    @patch("app.services.tv_provider_migration.migrate_tv_item_to_tvdb")
    def test_unresolvable_show_backs_off_instead_of_retrying_nightly(
        self,
        mock_migrate,
        _mock_enabled,
    ):
        self._create_tmdb_show(self.tvdb_user, "444")
        mock_migrate.return_value = TvMigrationResult(
            migrated=False,
            reason="no TVDB id resolvable",
        )

        first = migrate_tv_shows_to_preferred_provider_task()
        self.assertEqual(first["skipped"], 1)
        self.assertEqual(mock_migrate.call_count, 1)

        second = migrate_tv_shows_to_preferred_provider_task()
        self.assertEqual(second["skipped"], 0)
        self.assertEqual(mock_migrate.call_count, 1)

        state = MetadataBackfillState.objects.get(
            field=MetadataBackfillField.TVDB_MIGRATION.value,
        )
        # Backed off, never given up.
        self.assertFalse(state.give_up)
        self.assertIsNotNone(state.next_retry_at)

    @patch("app.providers.tvdb.enabled", return_value=True)
    @patch("app.services.tv_provider_migration.migrate_tv_item_to_tvdb")
    def test_a_backlog_no_longer_starves_a_newly_tracked_show(
        self,
        mock_migrate,
        _mock_enabled,
    ):
        for index in range(3):
            self._create_tmdb_show(self.tvdb_user, f"stuck-{index}")
        mock_migrate.return_value = TvMigrationResult(
            migrated=False,
            reason="no TVDB id resolvable",
        )

        migrate_tv_shows_to_preferred_provider_task(batch_size=3)
        mock_migrate.reset_mock()

        fresh = self._create_tmdb_show(self.tvdb_user, "brand-new")
        mock_migrate.return_value = TvMigrationResult(migrated=True)

        result = migrate_tv_shows_to_preferred_provider_task(batch_size=3)

        self.assertEqual(result["migrated"], 1)
        mock_migrate.assert_called_once()
        self.assertEqual(mock_migrate.call_args.args[0].pk, fresh.pk)

    @patch("app.providers.tvdb.enabled", return_value=True)
    @patch("app.services.tv_provider_migration.migrate_tv_item_to_tvdb")
    def test_a_crash_is_recorded_as_a_retryable_failure(
        self,
        mock_migrate,
        _mock_enabled,
    ):
        self._create_tmdb_show(self.tvdb_user, "444")
        mock_migrate.side_effect = Exception("boom")

        result = migrate_tv_shows_to_preferred_provider_task()

        self.assertEqual(result["errored"], 1)
        state = MetadataBackfillState.objects.get(
            field=MetadataBackfillField.TVDB_MIGRATION.value,
        )
        self.assertEqual(state.fail_count, 1)
        self.assertFalse(state.give_up)


class MigrationRetryHorizonTests(MigrateTvShowsToPreferredProviderTaskTests):
    """The backoff has to outlive the nightly beat that consumes it.

    The beat runs this daily and the shared backoff caps at one day, so a miss
    recorded on the default schedule is due again on the very next run - the
    backlog keeps filling the id-ordered batch and keeps starving newly
    tracked shows. These tests advance past the next scheduled run rather than
    re-running immediately.
    """

    @patch("app.providers.tvdb.enabled", return_value=True)
    @patch("app.services.tv_provider_migration.migrate_tv_item_to_tvdb")
    def test_miss_is_not_due_again_on_the_next_nightly_run(
        self,
        mock_migrate,
        _mock_enabled,
    ):
        self._create_tmdb_show(self.tvdb_user, "444")
        mock_migrate.return_value = TvMigrationResult(
            migrated=False,
            reason="no TVDB id resolvable",
        )

        migrate_tv_shows_to_preferred_provider_task()

        state = MetadataBackfillState.objects.get(
            field=MetadataBackfillField.TVDB_MIGRATION.value,
        )
        for night in range(1, 7):
            self.assertGreater(
                state.next_retry_at,
                timezone.now() + timedelta(days=night),
                f"backlog due again by night {night}",
            )

    @patch("app.providers.tvdb.enabled", return_value=True)
    @patch("app.services.tv_provider_migration.migrate_tv_item_to_tvdb")
    def test_backlog_does_not_starve_new_shows_across_a_week_of_runs(
        self,
        mock_migrate,
        _mock_enabled,
    ):
        for index in range(3):
            self._create_tmdb_show(self.tvdb_user, f"stuck-{index}")
        mock_migrate.return_value = TvMigrationResult(
            migrated=False,
            reason="no TVDB id resolvable",
        )
        migrate_tv_shows_to_preferred_provider_task(batch_size=3)

        fresh = self._create_tmdb_show(self.tvdb_user, "brand-new")
        mock_migrate.return_value = TvMigrationResult(migrated=True)

        # Simulate the next six nightly runs. The backlog must stay deferred
        # for every one of them, not just the first.
        for night in range(1, 7):
            with patch(
                "django.utils.timezone.now",
                return_value=timezone.now() + timedelta(days=night),
            ):
                candidates = list(_migration_candidates_queryset())
            self.assertEqual(
                [candidate.pk for candidate in candidates],
                [fresh.pk],
                f"backlog re-entered the batch on night {night}",
            )
