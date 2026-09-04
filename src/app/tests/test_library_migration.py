from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app.models import TV, Anime, Episode, Item, MediaTypes, Season, Sources, Status
from app.services.library_migration import (
    LibraryMigrationError,
    get_move_context,
    migrate_library_item,
)
from integrations import anime_mapping


def _show_metadata(media_id, title):
    return {
        "media_id": media_id,
        "source": Sources.TMDB.value,
        "media_type": MediaTypes.TV.value,
        "title": title,
        "original_title": title,
        "localized_title": title,
        "image": "https://example.com/show.jpg",
        "details": {},
        "related": {},
    }


class LibraryMigrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="library-move-user",
            password="password",
        )

    def _grouped_source(self, *, bucket=MediaTypes.TV.value):
        item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=bucket,
            title="Source Show",
        )
        tracker = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        return item, tracker

    @patch("app.services.library_migration.services.get_media_metadata")
    def test_grouped_move_preserves_episode_play_and_changes_identity(
        self,
        mock_get_metadata,
    ):
        source_item, source_tv = self._grouped_source()
        source_season_item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.TV.value,
            season_number=1,
            title="Source Season",
        )
        source_season = Season.objects.create(
            item=source_season_item,
            user=self.user,
            related_tv=source_tv,
            status=Status.IN_PROGRESS.value,
        )
        source_episode_item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.TV.value,
            season_number=1,
            episode_number=1,
            title="Source Episode",
        )
        Episode.objects.create(
            item=source_episode_item,
            related_season=source_season,
        )
        mock_get_metadata.side_effect = [
            _show_metadata("200", "Destination Show"),
            {
                "season/1": {
                    "season_number": 1,
                    "title": "Destination Season",
                    "episodes": [
                        {
                            "episode_number": 1,
                            "title": "Destination Episode",
                            "image": "https://example.com/episode.jpg",
                        },
                    ],
                },
            },
        ]

        target_item = migrate_library_item(
            self.user,
            source_item,
            MediaTypes.ANIME.value,
            Sources.TMDB.value,
            "200",
        )

        source_tv.refresh_from_db()
        moved_season = Season.objects.get(user=self.user, related_tv=source_tv)
        moved_episode = Episode.objects.get(related_season=moved_season)
        self.assertEqual(target_item.library_media_type, MediaTypes.ANIME.value)
        self.assertEqual(source_tv.item_id, target_item.id)
        self.assertEqual(moved_season.item.media_id, "200")
        self.assertEqual(moved_season.item.library_media_type, MediaTypes.ANIME.value)
        self.assertEqual(moved_episode.item.media_id, "200")
        self.assertEqual(
            moved_episode.item.library_media_type,
            MediaTypes.ANIME.value,
        )

    @patch("app.services.library_migration.services.get_media_metadata")
    def test_destination_tracker_wins_and_source_plays_are_retained(
        self,
        mock_get_metadata,
    ):
        source_item, source_tv = self._grouped_source()
        destination_item = Item.objects.create(
            media_id="200",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Destination Show",
        )
        destination_tv = TV.objects.create(
            item=destination_item,
            user=self.user,
            status=Status.PAUSED.value,
        )
        source_season_item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.TV.value,
            season_number=1,
            title="Source Season",
        )
        source_season = Season.objects.create(
            item=source_season_item,
            user=self.user,
            related_tv=source_tv,
        )
        destination_season_item = Item.objects.create(
            media_id="200",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.ANIME.value,
            season_number=1,
            title="Destination Season",
        )
        destination_season = Season.objects.create(
            item=destination_season_item,
            user=self.user,
            related_tv=destination_tv,
        )
        source_episode_item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.TV.value,
            season_number=1,
            episode_number=1,
            title="Source Episode",
        )
        destination_episode_item = Item.objects.create(
            media_id="200",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.ANIME.value,
            season_number=1,
            episode_number=1,
            title="Destination Episode",
        )
        Episode.objects.create(item=source_episode_item, related_season=source_season)
        Episode.objects.create(
            item=destination_episode_item,
            related_season=destination_season,
        )
        destination_tv.status = Status.PAUSED.value
        destination_tv.save_base(update_fields=["status"])
        mock_get_metadata.side_effect = [
            _show_metadata("200", "Destination Show"),
            {
                "season/1": {
                    "season_number": 1,
                    "title": "Destination Season",
                    "episodes": [{"episode_number": 1, "title": "Destination Episode"}],
                },
            },
        ]

        migrate_library_item(
            self.user,
            source_item,
            MediaTypes.ANIME.value,
            Sources.TMDB.value,
            "200",
        )

        destination_tv.refresh_from_db()
        self.assertEqual(destination_tv.status, Status.PAUSED.value)
        self.assertEqual(
            Episode.objects.filter(related_season=destination_season).count(),
            2,
        )
        self.assertFalse(TV.objects.filter(user=self.user, item=source_item).exists())

    @patch("app.services.library_migration.services.get_media_metadata")
    def test_grouped_to_flat_rejects_episode_state_atomically(self, mock_get_metadata):
        source_item, source_tv = self._grouped_source()
        source_season_item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.TV.value,
            season_number=1,
            title="Source Season",
        )
        Season.objects.create(
            item=source_season_item,
            user=self.user,
            related_tv=source_tv,
        )
        mock_get_metadata.return_value = {
            "media_id": "300",
            "title": "Flat Anime",
            "details": {},
            "related": {},
        }
        mock_get_metadata.reset_mock()

        with self.assertRaisesMessage(
            LibraryMigrationError,
            "cannot be represented safely",
        ):
            migrate_library_item(
                self.user,
                source_item,
                MediaTypes.ANIME.value,
                Sources.MAL.value,
                "300",
            )

        source_tv.refresh_from_db()
        self.assertEqual(source_tv.item_id, source_item.id)
        self.assertEqual(Season.objects.filter(related_tv=source_tv).count(), 1)
        self.assertEqual(mock_get_metadata.call_count, 1)


class LibraryMoveViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="library-move-view-user",
            password="password",
        )
        self.client.force_login(self.user)
        self.item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.TV.value,
            title="Source Show",
        )
        TV.objects.create(item=self.item, user=self.user)

    @patch("app.metadata_sync_views.services.search")
    def test_search_uses_destination_default_and_posts_explicit_identity(
        self,
        mock_search,
    ):
        self.user.anime_metadata_source_default = Sources.MAL.value
        self.user.save(update_fields=["anime_metadata_source_default"])
        mock_search.return_value = {
            "results": [
                {
                    "media_id": "200",
                    "title": "Anime Show",
                    "image": "https://example.com/anime.jpg",
                    "year": 2024,
                },
            ],
        }

        response = self.client.get(
            reverse(
                "search_library_move_candidates",
                kwargs={"item_id": self.item.id},
            ),
            {"q": "Anime"},
        )

        self.assertEqual(response.status_code, 200)
        mock_search.assert_called_once()
        self.assertContains(response, "Anime Show")
        self.assertContains(response, 'name="target_media_type" value="anime"')
        self.assertContains(response, 'name="target_source" value="mal"')
        self.assertContains(response, 'name="target_media_id" value="200"')

    def test_search_is_absent_when_destination_library_is_disabled(self):
        self.user.anime_enabled = False
        self.user.save(update_fields=["anime_enabled"])

        response = self.client.get(
            reverse(
                "search_library_move_candidates",
                kwargs={"item_id": self.item.id},
            ),
            {"q": "Anime"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "target_media_id")

    @patch.object(anime_mapping, "resolve_provider_series_id")
    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_flat_anime_search_falls_back_to_verified_grouped_provider(
        self,
        mock_resolve_provider_series_id,
    ):
        flat_item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Frieren",
        )
        Anime(
            item=flat_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        ).save_base()
        mock_resolve_provider_series_id.side_effect = lambda media_id, provider: (
            "424536" if provider == Sources.TVDB.value else None
        )

        context = get_move_context(self.user, flat_item)

        self.assertEqual(context["target_media_type"], MediaTypes.TV.value)
        self.assertEqual(context["target_source"], Sources.TVDB.value)
