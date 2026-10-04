"""Moving one user's tracked show between TMDB and TVDB (#1242)."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status
from app.services.library_migration import (
    LibraryMigrationError,
    preview_tv_provider_switch,
    switch_tv_provider,
    tv_provider_switch_target,
)


def _show_metadata(source, media_id, title):
    return {
        "media_id": media_id,
        "source": source,
        "media_type": MediaTypes.TV.value,
        "title": title,
        "original_title": title,
        "localized_title": title,
        "image": "https://example.com/show.jpg",
        "details": {},
        "related": {},
    }


def _season_payload(*episode_numbers):
    return {
        "season/1": {
            "season_number": 1,
            "title": "Season 1",
            "episodes": [
                {"episode_number": n, "title": f"Episode {n}"}
                for n in episode_numbers
            ],
        },
    }


class TvProviderSwitchTests(TestCase):
    """The move is per user, all-or-nothing, and works both ways."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="provider-switch",
            password="pw12345",
        )
        enabled = patch(
            "app.services.library_migration.metadata_resolution.provider_is_enabled",
            return_value=True,
        )
        enabled.start()
        self.addCleanup(enabled.stop)

    def _tracked_show(self, source, media_id, external_ids, *, episodes=(1, 2)):
        show = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.TV.value,
            title="Show",
            provider_external_ids=external_ids,
        )
        tv = TV.objects.create(
            item=show,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            score=8,
            notes="keep me",
        )
        season_item = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Season 1",
        )
        season = Season.objects.create(item=season_item, user=self.user, related_tv=tv)
        for number in episodes:
            episode_item = Item.objects.create(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=number,
                title=f"Episode {number}",
            )
            Episode.objects.create(item=episode_item, related_season=season)
        return show, tv

    def _metadata_mock(self, target_source, target_id, episode_numbers):
        def side_effect(media_type, media_id, source, *args, **kwargs):
            if media_type == "tv_with_seasons":
                return _season_payload(*episode_numbers)
            return _show_metadata(source, media_id, "Show (new)")

        return patch(
            "app.services.library_migration.services.get_media_metadata",
            side_effect=side_effect,
        )

    def test_target_is_the_other_provider(self):
        tmdb_show, _ = self._tracked_show(
            Sources.TMDB.value, "1396", {"tvdb_id": "81189"}
        )
        self.assertEqual(
            tv_provider_switch_target(self.user, tmdb_show),
            Sources.TVDB.value,
        )

    def test_not_offered_for_a_show_the_user_does_not_track(self):
        other = get_user_model().objects.create_user(username="other", password="pw")
        show, _ = self._tracked_show(Sources.TMDB.value, "1396", {"tvdb_id": "81189"})
        self.assertIsNone(tv_provider_switch_target(other, show))

    def test_tmdb_to_tvdb_keeps_tracking_and_watched_episodes(self):
        show, tv = self._tracked_show(Sources.TMDB.value, "1396", {"tvdb_id": "81189"})

        with self._metadata_mock(Sources.TVDB.value, "81189", (1, 2, 3)):
            target = switch_tv_provider(self.user, show)

        self.assertEqual(target.source, Sources.TVDB.value)
        self.assertEqual(target.media_id, "81189")
        tv.refresh_from_db()
        self.assertEqual(tv.item_id, target.pk)
        self.assertEqual(tv.score, 8)
        self.assertEqual(tv.notes, "keep me")
        episodes = Episode.objects.filter(related_season__related_tv=tv)
        self.assertEqual(episodes.count(), 2)
        self.assertTrue(
            all(e.item.source == Sources.TVDB.value for e in episodes.select_related("item")),
        )

    def test_tvdb_to_tmdb_works_in_reverse(self):
        show, tv = self._tracked_show(Sources.TVDB.value, "81189", {"tmdb_id": "1396"})

        with self._metadata_mock(Sources.TMDB.value, "1396", (1, 2)):
            target = switch_tv_provider(self.user, show)

        self.assertEqual((target.source, target.media_id), (Sources.TMDB.value, "1396"))
        tv.refresh_from_db()
        self.assertEqual(tv.item_id, target.pk)
        self.assertEqual(
            Episode.objects.filter(related_season__related_tv=tv).count(),
            2,
        )

    def test_missing_episode_blocks_the_move_and_changes_nothing(self):
        show, tv = self._tracked_show(Sources.TMDB.value, "1396", {"tvdb_id": "81189"})

        with self._metadata_mock(Sources.TVDB.value, "81189", (1,)):
            preview = preview_tv_provider_switch(self.user, show)
            self.assertEqual(preview.missing, ["1x2"])
            self.assertIsNone(preview.plan)
            with self.assertRaises(LibraryMigrationError):
                switch_tv_provider(self.user, show)

        tv.refresh_from_db()
        self.assertEqual(tv.item_id, show.pk)
        self.assertEqual(
            Episode.objects.filter(
                related_season__related_tv=tv,
                item__source=Sources.TMDB.value,
            ).count(),
            2,
        )

    def test_other_users_keep_the_original_show(self):
        other = get_user_model().objects.create_user(username="other", password="pw")
        show, _ = self._tracked_show(Sources.TMDB.value, "1396", {"tvdb_id": "81189"})
        other_tv = TV.objects.create(item=show, user=other, status=Status.PLANNING.value)

        with self._metadata_mock(Sources.TVDB.value, "81189", (1, 2)):
            switch_tv_provider(self.user, show)

        other_tv.refresh_from_db()
        self.assertEqual(other_tv.item_id, show.pk)
        show.refresh_from_db()
        self.assertEqual(show.source, Sources.TMDB.value)

    def test_no_known_match_is_reported_not_guessed(self):
        show, _ = self._tracked_show(Sources.TMDB.value, "1396", {})

        with (
            patch(
                "app.services.library_migration.tmdb.resolve_tvdb_id_for_tmdb_show",
                return_value=None,
            ),
            self.assertRaisesMessage(LibraryMigrationError, "No matching TheTVDB title"),
        ):
            preview_tv_provider_switch(self.user, show)

    def test_view_previews_then_applies(self):
        show, tv = self._tracked_show(Sources.TMDB.value, "1396", {"tvdb_id": "81189"})
        self.client.force_login(self.user)
        url = reverse("switch_tv_provider", args=[show.pk])

        with self._metadata_mock(Sources.TVDB.value, "81189", (1, 2)):
            preview = self.client.get(url)
            self.assertContains(preview, "Move to TheTVDB")
            tv.refresh_from_db()
            self.assertEqual(tv.item_id, show.pk)

            response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        tv.refresh_from_db()
        self.assertEqual(tv.item.source, Sources.TVDB.value)

    def test_view_lists_what_is_missing(self):
        show, _ = self._tracked_show(Sources.TMDB.value, "1396", {"tvdb_id": "81189"})
        self.client.force_login(self.user)

        with self._metadata_mock(Sources.TVDB.value, "81189", (1,)):
            response = self.client.get(reverse("switch_tv_provider", args=[show.pk]))

        self.assertContains(response, "1x2")
        self.assertContains(response, "disabled")


@override_settings(TVDB_API_KEY="test-tvdb-key")
class TvProviderPromptTests(TestCase):
    """Switching the TV default asks before moving, and "leave" really leaves."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="provider-prompt",
            password="pw12345",
        )
        self.client.force_login(self.user)

    def _track(self, source, media_id="1396"):
        item = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.TV.value,
            title=f"Show {media_id}",
        )
        TV.objects.create(item=item, user=self.user, status=Status.IN_PROGRESS.value)
        return item

    def _switch_default(self, source):
        return self.client.post(
            reverse("set_media_type_provider", args=[MediaTypes.TV.value]),
            {"source": source},
            follow=True,
        )

    def test_prompt_appears_once_and_pauses_the_nightly_move(self):
        self._track(Sources.TMDB.value)

        response = self._switch_default(Sources.TVDB.value)

        self.assertContains(response, "Move your tracked shows to")
        self.assertContains(response, "Leave my library as it is")
        self.assertContains(response, reverse("convert_tv_library"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.tv_auto_move_to_default_provider)
        again = self.client.get(reverse("metadata_settings"))
        self.assertNotContains(again, "Move your tracked shows to")

    def test_no_prompt_and_flag_untouched_when_nothing_is_on_the_other_provider(self):
        self._track(Sources.TVDB.value, "81189")

        response = self._switch_default(Sources.TVDB.value)

        self.assertNotContains(response, "Move your tracked shows to")
        self.user.refresh_from_db()
        self.assertTrue(self.user.tv_auto_move_to_default_provider)

    def test_leaving_the_library_keeps_it_out_of_the_nightly_job(self):
        from app.tasks_tv_provider_migration import _migration_candidates_queryset

        item = self._track(Sources.TMDB.value)
        self._switch_default(Sources.TVDB.value)

        self.assertNotIn(item, _migration_candidates_queryset())

    def test_move_endpoint_queues_the_task_and_turns_nightly_back_on(self):
        self.user.tv_auto_move_to_default_provider = False
        self.user.save(update_fields=["tv_auto_move_to_default_provider"])

        with patch(
            "app.tasks_tv_provider_migration.move_user_tv_library_task.delay",
        ) as mock_delay:
            response = self.client.post(reverse("convert_tv_library"))

        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once_with(self.user.id)
        self.user.refresh_from_db()
        self.assertTrue(self.user.tv_auto_move_to_default_provider)

    def test_task_moves_what_it_can_and_names_what_it_leaves(self):
        from app.tasks_tv_provider_migration import move_user_tv_library_task

        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save(update_fields=["tv_metadata_source_default"])
        movable = self._track(Sources.TMDB.value, "1")
        blocked = self._track(Sources.TMDB.value, "2")
        self._track(Sources.TVDB.value, "3")  # already on the default provider

        def fake_switch(user, item):
            if item.pk == blocked.pk:
                raise LibraryMigrationError("missing episodes")
            return item

        with patch(
            "app.services.library_migration.switch_tv_provider",
            side_effect=fake_switch,
        ) as mock_switch:
            result = move_user_tv_library_task(self.user.id)

        self.assertEqual(result, {"moved": 1, "skipped": 1, "unresolved": [blocked.title]})
        self.assertEqual(
            {call.args[1].pk for call in mock_switch.call_args_list},
            {movable.pk, blocked.pk},
        )
