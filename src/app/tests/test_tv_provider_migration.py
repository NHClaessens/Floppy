from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status
from app.services.tv_provider_migration import migrate_tv_item_to_tvdb


class TvProviderMigrationTests(TestCase):
    """Tests for in-place TMDB -> TVDB show migration (#387)."""

    def setUp(self):
        """Create a TMDB-tracked show/season/episode with a cached TVDB id."""
        self.user = get_user_model().objects.create_user(
            username="tv-migrate",
            password="pw12345",
        )
        self.tvdb_enabled_patcher = patch(
            "app.services.tv_provider_migration.tvdb.enabled",
            return_value=True,
        )
        self.tvdb_enabled_patcher.start()
        self.addCleanup(self.tvdb_enabled_patcher.stop)

        self.show_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/show.jpg",
            provider_external_ids={"tvdb_id": "81189"},
        )
        TV.objects.create(
            item=self.show_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        self.season_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Breaking Bad",
            image="https://example.com/season1.jpg",
        )
        season = Season.objects.create(
            item=self.season_item,
            user=self.user,
            related_tv=TV.objects.get(item=self.show_item),
            status=Status.IN_PROGRESS.value,
        )
        self.episode_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Pilot",
            image="",
        )
        Episode.objects.create(
            item=self.episode_item,
            related_season=season,
            end_date=None,
        )

    def _tvdb_payload(self, episode_numbers=(1, 2), episode_titles=None):
        episode_titles = episode_titles or {}
        return {
            "media_id": "81189",
            "title": "Breaking Bad",
            "image": "https://example.com/tvdb-show.jpg",
            "season/1": {
                "image": "https://example.com/tvdb-season1.jpg",
                "episodes": [
                    {
                        "episode_number": n,
                        **({"name": episode_titles[n]} if n in episode_titles else {}),
                    }
                    for n in episode_numbers
                ],
            },
        }

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_migrates_in_place_when_structure_matches(self, mock_tv_with_seasons):
        """Show/season/episode Items are re-keyed without touching history rows."""
        mock_tv_with_seasons.return_value = self._tvdb_payload()

        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertTrue(result.migrated)
        self.show_item.refresh_from_db()
        self.season_item.refresh_from_db()
        self.episode_item.refresh_from_db()
        self.assertEqual(self.show_item.source, Sources.TVDB.value)
        self.assertEqual(self.show_item.media_id, "81189")
        self.assertEqual(self.season_item.source, Sources.TVDB.value)
        self.assertEqual(self.season_item.media_id, "81189")
        self.assertEqual(self.episode_item.source, Sources.TVDB.value)
        self.assertEqual(self.episode_item.media_id, "81189")
        # Existing Episode/Season/TV rows keep pointing at the same Item PKs.
        self.assertTrue(
            Episode.objects.filter(item=self.episode_item).exists(),
        )

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_migration_does_not_sweep_duplicate_bucket_items(
        self,
        mock_tv_with_seasons,
    ):
        """A season/episode row in a different library_media_type bucket is untouched.

        Item allows two rows to share media_id/source/media_type/season/episode
        as long as library_media_type differs (issue #1147). Migrating the TV
        bucket's show must not re-key a duplicate row that belongs to another
        bucket (e.g. grouped anime), or the two collide under identical TVDB
        coordinates afterwards.
        """
        mock_tv_with_seasons.return_value = self._tvdb_payload()

        other_bucket_season = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.ANIME.value,
            season_number=1,
            title="Breaking Bad",
        )
        other_bucket_episode = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.ANIME.value,
            season_number=1,
            episode_number=1,
            title="Breaking Bad",
        )

        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertTrue(result.migrated)
        other_bucket_season.refresh_from_db()
        other_bucket_episode.refresh_from_db()
        self.assertEqual(other_bucket_season.source, Sources.TMDB.value)
        self.assertEqual(other_bucket_season.media_id, "1396")
        self.assertEqual(other_bucket_episode.source, Sources.TMDB.value)
        self.assertEqual(other_bucket_episode.media_id, "1396")

    @patch("app.services.tv_provider_migration.history_cache.invalidate_history_cache")
    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_migration_repairs_episode_title_and_history_cache(
        self,
        mock_tv_with_seasons,
        mock_invalidate_history_cache,
    ):
        """TVDB's episode title replaces a stale show title after rekeying."""
        mock_tv_with_seasons.return_value = self._tvdb_payload(
            episode_titles={1: "Pilot from TVDB"},
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertTrue(result.migrated)
        self.episode_item.refresh_from_db()
        self.assertEqual(self.episode_item.title, "Pilot from TVDB")
        self.assertIsNone(self.episode_item.original_title)
        self.assertEqual(self.episode_item.localized_title, "Pilot from TVDB")
        mock_invalidate_history_cache.assert_called_once_with(
            self.user.id,
            force=True,
        )

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_migration_preserves_episode_title_without_provider_title(
        self,
        mock_tv_with_seasons,
    ):
        """Missing TVDB episode names do not erase existing title fields."""
        self.episode_item.original_title = "Pilot Original"
        self.episode_item.localized_title = "Pilot Localized"
        self.episode_item.save(
            update_fields=["original_title", "localized_title"],
        )
        mock_tv_with_seasons.return_value = self._tvdb_payload(
            episode_titles={1: ""},
        )

        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertTrue(result.migrated)
        self.episode_item.refresh_from_db()
        self.assertEqual(self.episode_item.title, "Pilot")
        self.assertEqual(self.episode_item.original_title, "Pilot Original")
        self.assertEqual(self.episode_item.localized_title, "Pilot Localized")

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_pins_instead_of_migrating_when_episode_missing_on_tvdb(
        self,
        mock_tv_with_seasons,
    ):
        """A locally-watched episode absent from TVDB must block migration."""
        mock_tv_with_seasons.return_value = self._tvdb_payload(episode_numbers=(2, 3))

        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertFalse(result.migrated)
        self.show_item.refresh_from_db()
        self.assertEqual(self.show_item.source, Sources.TMDB.value)
        self.assertIsNotNone(self.show_item.metadata_migration_pinned_at)

    def test_skips_when_no_tvdb_id_resolvable(self):
        """No TVDB id available means skip, not pin — worth retrying later."""
        self.show_item.provider_external_ids = {}
        self.show_item.save(update_fields=["provider_external_ids"])

        with patch(
            "app.services.tv_provider_migration.tmdb.resolve_tvdb_id_for_tmdb_show",
            return_value=None,
        ):
            result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertFalse(result.migrated)
        self.show_item.refresh_from_db()
        self.assertIsNone(self.show_item.metadata_migration_pinned_at)

    def test_skips_grouped_anime_items(self):
        """Grouped-anime items use a separate TVDB pathway and must not be touched."""
        self.show_item.library_media_type = MediaTypes.ANIME.value
        self.show_item.save(update_fields=["library_media_type"])

        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertFalse(result.migrated)

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_merges_into_existing_tvdb_item_on_collision(
        self,
        mock_tv_with_seasons,
    ):
        """A verified TVDB counterpart absorbs the TMDB duplicate instead of pinning."""
        mock_tv_with_seasons.return_value = self._tvdb_payload()

        existing_show = Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="",
        )

        tmdb_item_pk = self.show_item.pk
        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertTrue(result.migrated)
        self.assertFalse(Item.objects.filter(pk=tmdb_item_pk).exists())
        self.assertTrue(
            TV.objects.filter(item=existing_show, user=self.user).exists(),
        )
        # Season/episode Items with no TVDB counterpart are re-keyed in place.
        self.season_item.refresh_from_db()
        self.episode_item.refresh_from_db()
        self.assertEqual(self.season_item.source, Sources.TVDB.value)
        self.assertEqual(self.episode_item.source, Sources.TVDB.value)
        self.assertTrue(Episode.objects.filter(item=self.episode_item).exists())

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_collision_merge_repairs_surviving_episode_title(
        self,
        mock_tv_with_seasons,
    ):
        """A TVDB episode keeper also receives the canonical provider title."""
        mock_tv_with_seasons.return_value = self._tvdb_payload(
            episode_titles={1: "Pilot from TVDB"},
        )

        existing_show = Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="",
        )
        existing_episode = Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Breaking Bad",
            image="",
        )

        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertTrue(result.migrated)
        existing_episode.refresh_from_db()
        self.assertEqual(existing_episode.title, "Pilot from TVDB")
        self.assertTrue(Episode.objects.filter(item=existing_episode).exists())

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_merge_folds_colliding_season_onto_its_tvdb_counterpart(
        self,
        mock_tv_with_seasons,
    ):
        """A season that also already exists under TVDB gets merged, not re-keyed."""
        mock_tv_with_seasons.return_value = self._tvdb_payload()

        existing_show = Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="",
        )
        existing_season = Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Breaking Bad",
            image="",
        )

        season_item_pk = self.season_item.pk
        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertTrue(result.migrated)
        self.assertFalse(Item.objects.filter(pk=season_item_pk).exists())
        self.assertTrue(
            Season.objects.filter(
                item=existing_season,
                related_tv=TV.objects.get(item=existing_show),
            ).exists(),
        )
        self.episode_item.refresh_from_db()
        self.assertEqual(self.episode_item.source, Sources.TVDB.value)

    @patch("app.services.tv_provider_migration.tvdb.tv_with_seasons")
    def test_pins_when_collision_and_structure_incompatible(
        self,
        mock_tv_with_seasons,
    ):
        """A structurally-incompatible collision still pins instead of merging."""
        mock_tv_with_seasons.return_value = self._tvdb_payload(episode_numbers=(2, 3))
        Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="",
        )

        result = migrate_tv_item_to_tvdb(self.show_item)

        self.assertFalse(result.migrated)
        self.show_item.refresh_from_db()
        self.assertEqual(self.show_item.source, Sources.TMDB.value)
        self.assertIsNotNone(self.show_item.metadata_migration_pinned_at)
