"""Converged-work regressions for the IMDB game credits pipeline.

Production evidence (2026-09-14): one run of "Refresh IMDB game credits from
datasets" held a background worker for 698 seconds, searched 1,966 people,
matched 1,048 of them, and updated zero rows - then did the same thing again on
the next run, because nothing recorded that the question had already been
asked. These tests pin the freshness state that stops the repeat.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from app.models import (
    PERSON_PROFILE_BACKFILL_VERSION,
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Person,
    PersonGender,
    Sources,
)
from app.providers.services import ProviderAPIError
from app.services import imdb_game_credits


def _person(name="Alice Actor", person_id="nm0000001"):
    return Person.objects.create(
        source=Sources.IMDB.value,
        source_person_id=person_id,
        name=name,
        image="",
    )


def _game(title="Dispatch", media_id="igdb-1", **kwargs):
    return Item.objects.create(
        media_id=media_id,
        source=Sources.IGDB.value,
        media_type=MediaTypes.GAME.value,
        title=title,
        **kwargs,
    )


class PersonProfileBackfillFreshnessTests(TestCase):
    def test_completed_lookup_with_no_match_is_not_searched_again(self):
        """The dominant production case: TMDB has never heard of this person."""
        person = _person()

        with patch(
            "app.providers.tmdb.search_person_profile",
            return_value=None,
        ) as search:
            self.assertEqual(imdb_game_credits.backfill_missing_person_profiles(), 0)
            self.assertEqual(search.call_count, 1)

            # Second run: still missing an image, but nothing new to ask.
            self.assertEqual(imdb_game_credits.backfill_missing_person_profiles(), 0)
            self.assertEqual(search.call_count, 1)

        person.refresh_from_db()
        self.assertEqual(person.image, "")
        self.assertEqual(
            person.profile_backfill_version,
            PERSON_PROFILE_BACKFILL_VERSION,
        )
        self.assertIsNone(person.profile_backfill_next_retry_at)

    def test_match_that_fills_nothing_is_also_terminal(self):
        """1,048 of 1,966 matched and still updated nothing - same conclusion."""
        _person()

        with patch(
            "app.providers.tmdb.search_person_profile",
            return_value={"image": "", "gender": "unknown"},
        ) as search:
            imdb_game_credits.backfill_missing_person_profiles()
            imdb_game_credits.backfill_missing_person_profiles()

        self.assertEqual(search.call_count, 1)

    def test_partial_fill_still_converges(self):
        """An image with no gender is as complete as TMDB can make this row."""
        person = _person()

        with patch(
            "app.providers.tmdb.search_person_profile",
            return_value={"image": "https://image.tmdb.org/alice.jpg"},
        ) as search:
            self.assertEqual(imdb_game_credits.backfill_missing_person_profiles(), 1)
            self.assertEqual(imdb_game_credits.backfill_missing_person_profiles(), 0)

        self.assertEqual(search.call_count, 1)
        person.refresh_from_db()
        self.assertEqual(person.image, "https://image.tmdb.org/alice.jpg")
        self.assertEqual(person.gender, PersonGender.UNKNOWN.value)

    def test_transient_provider_failure_stays_retryable(self):
        person = _person()

        with patch(
            "app.providers.tmdb.search_person_profile",
            side_effect=ProviderAPIError(Sources.TMDB.value, Exception("503")),
        ) as search:
            imdb_game_credits.backfill_missing_person_profiles()
            self.assertEqual(search.call_count, 1)

            # Backed off, so an immediate re-run must not re-ask.
            imdb_game_credits.backfill_missing_person_profiles()
            self.assertEqual(search.call_count, 1)

        person.refresh_from_db()
        self.assertEqual(person.profile_backfill_version, 0)
        self.assertEqual(person.profile_backfill_fail_count, 1)
        self.assertIsNotNone(person.profile_backfill_next_retry_at)

        # Once the retry falls due the person is a candidate again.
        Person.objects.filter(pk=person.pk).update(
            profile_backfill_next_retry_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(
            list(imdb_game_credits.people_needing_profile_backfill()),
            [person],
        )

    def test_name_change_reopens_the_lookup(self):
        person = _person()

        with patch("app.providers.tmdb.search_person_profile", return_value=None):
            imdb_game_credits.backfill_missing_person_profiles()

        Person.objects.filter(pk=person.pk).update(name="Alice B. Actor")

        with patch(
            "app.providers.tmdb.search_person_profile",
            return_value=None,
        ) as search:
            imdb_game_credits.backfill_missing_person_profiles()

        search.assert_called_once_with("Alice B. Actor")

    def test_version_bump_reopens_the_lookup(self):
        _person()

        with patch("app.providers.tmdb.search_person_profile", return_value=None):
            imdb_game_credits.backfill_missing_person_profiles()

        with (
            patch(
                "app.services.imdb_game_credits.PERSON_PROFILE_BACKFILL_VERSION",
                PERSON_PROFILE_BACKFILL_VERSION + 1,
            ),
            patch(
                "app.providers.tmdb.search_person_profile",
                return_value=None,
            ) as search,
        ):
            imdb_game_credits.backfill_missing_person_profiles()

        self.assertEqual(search.call_count, 1)

    def test_missing_profile_count_gates_the_startup_sweep(self):
        """``count_people_missing_profiles`` gates whether the 698s task runs."""
        _person()
        self.assertEqual(imdb_game_credits.count_people_missing_profiles(), 1)

        with patch("app.providers.tmdb.search_person_profile", return_value=None):
            imdb_game_credits.backfill_missing_person_profiles()

        self.assertEqual(imdb_game_credits.count_people_missing_profiles(), 0)


class GameStudioBackfillFreshnessTests(TestCase):
    def test_game_without_igdb_studios_is_not_refetched_next_run(self):
        item = _game(provider_external_ids={"imdb_id": "tt1111111"})

        with patch(
            "app.providers.services.get_media_metadata",
            return_value={"studios_full": []},
        ) as fetch:
            imdb_game_credits.backfill_missing_game_studios()
            imdb_game_credits.backfill_missing_game_studios()

        self.assertEqual(fetch.call_count, 1)
        state = MetadataBackfillState.objects.get(
            item=item,
            field=MetadataBackfillField.STUDIOS.value,
        )
        # Pending, not given up: IGDB can add companies later.
        self.assertFalse(state.give_up)
        self.assertIsNotNone(state.next_retry_at)

    def test_provider_error_is_recorded_as_a_failure(self):
        item = _game(provider_external_ids={"imdb_id": "tt1111111"})

        with patch(
            "app.providers.services.get_media_metadata",
            side_effect=ProviderAPIError(Sources.IGDB.value, Exception("boom")),
        ):
            imdb_game_credits.backfill_missing_game_studios()

        state = MetadataBackfillState.objects.get(
            item=item,
            field=MetadataBackfillField.STUDIOS.value,
        )
        self.assertEqual(state.fail_count, 1)


class ImdbTitleMatchFreshnessTests(TestCase):
    def test_unmatched_game_does_not_redownload_the_title_index(self):
        """One candidate is enough to pull the whole title.basics dataset."""
        _game(title="Some Obscure Game")

        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value={},
        ) as download:
            self.assertEqual(imdb_game_credits.resolve_game_imdb_ids(), 0)
            self.assertEqual(imdb_game_credits.resolve_game_imdb_ids(), 0)

        download.assert_called_once_with()

    def test_ambiguous_match_is_recorded_rather_than_silently_dropped(self):
        item = _game(title="Dispatch")

        index = {
            "tt1111111": ("Dispatch", 2024),
            "tt2222222": ("Dispatch", 2024),
        }
        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value=index,
        ):
            self.assertEqual(imdb_game_credits.resolve_game_imdb_ids(), 0)

        state = MetadataBackfillState.objects.get(
            item=item,
            field=MetadataBackfillField.IMDB_MATCH.value,
        )
        self.assertFalse(state.give_up)
        self.assertIsNotNone(state.next_retry_at)

    def test_unambiguous_match_still_resolves(self):
        item = _game(title="Dispatch")

        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value={"tt1111111": ("Dispatch", 2024)},
        ):
            self.assertEqual(imdb_game_credits.resolve_game_imdb_ids(), 1)

        item.refresh_from_db()
        self.assertEqual(item.provider_external_ids["imdb_id"], "tt1111111")


class ImdbMatchRetryHorizonTests(TestCase):
    """A backoff shorter than the schedule that consumes it defers nothing.

    The nightly quality task queues this work once a day and the shared
    backoff caps at one day, so a miss recorded on the default schedule is due
    again on the very next nightly run - and one eligible candidate is enough
    to re-download and re-parse the whole title.basics dataset. These tests
    advance past the next scheduled run rather than re-running immediately.
    """

    NIGHTLY = timedelta(days=1)

    def test_miss_is_not_due_again_on_the_next_nightly_run(self):
        _game(title="Some Obscure Game")

        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value={},
        ) as download:
            imdb_game_credits.resolve_game_imdb_ids()

            # Walk forward a night at a time across a week of nightly runs.
            for night in range(1, 7):
                state = MetadataBackfillState.objects.get(
                    field=MetadataBackfillField.IMDB_MATCH.value,
                )
                self.assertGreater(
                    state.next_retry_at,
                    timezone.now() + self.NIGHTLY * night,
                    f"due again by night {night}",
                )

        download.assert_called_once_with()

    def test_a_newly_tracked_game_is_still_picked_up_immediately(self):
        """The floor must defer known misses, not block new candidates."""
        _game(title="Some Obscure Game")

        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value={},
        ):
            imdb_game_credits.resolve_game_imdb_ids()

        _game(title="Brand New Game", media_id="igdb-2")

        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value={"tt2222222": ("Brand New Game", 2026)},
        ) as download:
            self.assertEqual(imdb_game_credits.resolve_game_imdb_ids(), 1)

        download.assert_called_once_with()

    def test_the_miss_does_come_back_eventually(self):
        """Deferred, not given up: IMDB can publish the title later."""
        _game(title="Some Obscure Game")

        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value={},
        ):
            imdb_game_credits.resolve_game_imdb_ids()

        state = MetadataBackfillState.objects.get(
            field=MetadataBackfillField.IMDB_MATCH.value,
        )
        self.assertFalse(state.give_up)
        MetadataBackfillState.objects.filter(pk=state.pk).update(
            next_retry_at=timezone.now() - timedelta(minutes=1),
        )

        with patch(
            "app.providers.imdb_datasets.download_videogame_title_index",
            return_value={},
        ) as download:
            imdb_game_credits.resolve_game_imdb_ids()

        download.assert_called_once_with()


class StudioRetryHorizonTests(TestCase):
    def test_studio_miss_is_not_due_again_on_the_next_nightly_run(self):
        _game(provider_external_ids={"imdb_id": "tt1111111"})

        with patch(
            "app.providers.services.get_media_metadata",
            return_value={"studios_full": []},
        ):
            imdb_game_credits.backfill_missing_game_studios()

        state = MetadataBackfillState.objects.get(
            field=MetadataBackfillField.STUDIOS.value,
        )
        self.assertGreater(
            state.next_retry_at,
            timezone.now() + timedelta(days=1),
        )
