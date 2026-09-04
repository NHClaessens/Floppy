"""The external-ID catch-up sweep repairs existing libraries and then stops.

The provider fix in `tmdb.movie()` only helps titles added after it, because
items never refetch on their own. This sweep is the repair path for libraries
populated while the IMDb ID was being discarded (issue #1066).
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from app import backfill_queue, tasks_external_ids
from app.models import (
    BackfillReconcileState,
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Sources,
)
from app.tasks_external_ids import (
    EXTERNAL_IDS_BACKFILL_VERSION,
    _external_ids_queryset,
    is_external_ids_backfill_reconcile_complete,
    populate_external_ids_for_items,
    reconcile_external_ids_backfill,
)


def make_movie(media_id, provider_external_ids=None):
    """Create a TMDB movie, by default with no resolved IMDb ID."""
    return Item.objects.create(
        media_id=str(media_id),
        source=Sources.TMDB.value,
        media_type=MediaTypes.MOVIE.value,
        title=f"Movie {media_id}",
        provider_external_ids=provider_external_ids or {},
    )


def movie_metadata(media_id, imdb_id="tt0137523"):
    """A minimal TMDB movie payload in the post-fix shape."""
    external_ids = {"imdb_id": imdb_id} if imdb_id else {}
    return {
        "media_id": str(media_id),
        "source": Sources.TMDB.value,
        "media_type": MediaTypes.MOVIE.value,
        "provider_external_ids": external_ids,
        "details": {},
    }


class ExternalIDsQuerysetTests(TestCase):
    """The candidate set is the movies the Stremio catalog has to drop."""

    def test_movies_without_imdb_id_are_candidates(self):
        item = make_movie(550)

        self.assertIn(item.id, set(_external_ids_queryset().values_list("id", flat=True)))

    def test_movies_with_imdb_id_are_not_candidates(self):
        item = make_movie(551, {"imdb_id": "tt0000001", "tmdb_id": "551"})

        self.assertNotIn(
            item.id,
            set(_external_ids_queryset().values_list("id", flat=True)),
        )

    def test_a_bare_tmdb_id_is_not_enough(self):
        """upsert_provider_links synthesises tmdb_id from media_id for free."""
        item = make_movie(552, {"tmdb_id": "552"})

        self.assertIn(item.id, set(_external_ids_queryset().values_list("id", flat=True)))

    def test_non_tmdb_sources_are_left_alone(self):
        item = Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
        )

        self.assertNotIn(
            item.id,
            set(_external_ids_queryset().values_list("id", flat=True)),
        )


class PopulateExternalIDsTests(TestCase):
    """Fetching writes the IMDb ID and records the attempt."""

    @patch("app.providers.services.get_media_metadata")
    def test_populates_imdb_id_onto_the_item(self, mock_get_media_metadata):
        item = make_movie(550)
        mock_get_media_metadata.return_value = movie_metadata(550)

        result = populate_external_ids_for_items([item.id])

        item.refresh_from_db()
        self.assertEqual(item.provider_external_ids["imdb_id"], "tt0137523")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["errors"], 0)

    @patch("app.providers.services.get_media_metadata")
    def test_item_drops_out_of_the_candidate_set_once_resolved(
        self,
        mock_get_media_metadata,
    ):
        item = make_movie(550)
        mock_get_media_metadata.return_value = movie_metadata(550)

        populate_external_ids_for_items([item.id])

        self.assertNotIn(
            item.id,
            set(_external_ids_queryset().values_list("id", flat=True)),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_a_title_tmdb_has_no_imdb_id_for_still_converges(
        self,
        mock_get_media_metadata,
    ):
        """Unlike watch providers, a missing IMDb ID is usually permanent.

        Recording it as pending would leave the item retrying forever and the
        sweep would never be able to report itself complete.
        """
        item = make_movie(550)
        mock_get_media_metadata.return_value = movie_metadata(550, imdb_id=None)

        result = populate_external_ids_for_items([item.id])

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["errors"], 0)
        state = MetadataBackfillState.objects.get(
            item=item,
            field=MetadataBackfillField.EXTERNAL_IDS,
        )
        self.assertIsNotNone(state.last_success_at)
        self.assertEqual(state.fail_count, 0)
        self.assertFalse(state.give_up)
        self.assertNotIn(
            item.id,
            set(_external_ids_queryset().values_list("id", flat=True)),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_a_failed_fetch_is_recorded_for_retry(self, mock_get_media_metadata):
        item = make_movie(550)
        mock_get_media_metadata.side_effect = ValueError("boom")

        result = populate_external_ids_for_items([item.id])

        self.assertEqual(result["errors"], 1)
        state = MetadataBackfillState.objects.get(
            item=item,
            field=MetadataBackfillField.EXTERNAL_IDS,
        )
        self.assertEqual(state.fail_count, 1)
        self.assertIsNone(state.last_success_at)
        self.assertIsNotNone(state.next_retry_at)


@patch("app.tasks_external_ids.populate_external_ids_backfill_queue.apply_async")
class ExternalIDsReconcileTests(TestCase):
    """The sweep enqueues the library once and then reports completion."""

    def setUp(self):
        cache.clear()
        backfill_queue.clear(
            tasks_external_ids.EXTERNAL_IDS_BACKFILL_ITEMS_QUEUE_KEY,
            tasks_external_ids.EXTERNAL_IDS_BACKFILL_ITEMS_SCHEDULED_KEY,
        )
        BackfillReconcileState.objects.all().delete()

    def test_reconcile_enqueues_outstanding_movies(self, _mock_apply_async):
        make_movie(550)
        make_movie(551)

        result = reconcile_external_ids_backfill()

        self.assertEqual(result["selected"], 2)
        self.assertEqual(result["enqueued"], 2)

    def test_an_empty_library_reports_complete(self, _mock_apply_async):
        result = reconcile_external_ids_backfill()

        self.assertTrue(result["complete"])
        self.assertTrue(is_external_ids_backfill_reconcile_complete())
        state = BackfillReconcileState.objects.get(key=tasks_external_ids.RECONCILE_KEY)
        self.assertEqual(state.strategy_version, EXTERNAL_IDS_BACKFILL_VERSION)

    @patch("app.providers.services.get_media_metadata")
    def test_reconcile_completes_after_the_library_is_resolved(
        self,
        mock_get_media_metadata,
        _mock_apply_async,
    ):
        item = make_movie(550)
        mock_get_media_metadata.return_value = movie_metadata(550)
        populate_external_ids_for_items([item.id])

        # One pass to walk to the end of the table, a second to conclude that
        # starting from the top still finds nothing.
        reconcile_external_ids_backfill()
        result = reconcile_external_ids_backfill()

        self.assertTrue(result["complete"])


class BackfillItemMetadataCommandTests(TestCase):
    """The documented terminal repair route has to actually repair this."""

    @patch("app.providers.services.get_media_metadata")
    def test_force_run_persists_provider_external_ids(self, mock_get_media_metadata):
        """apply_item_metadata() covers neither CORE_ nor PROVIDER_ external IDs.

        Before this the command fetched the payload, wrote the fields it knew
        about, and dropped the IMDb ID on the floor - so `--force` looked like a
        repair and wasn't one.
        """
        from django.core.management import call_command

        item = make_movie(550)
        mock_get_media_metadata.return_value = movie_metadata(550)

        call_command("backfill_item_metadata", "--media-type", "movie", "--force")

        item.refresh_from_db()
        self.assertEqual(item.provider_external_ids["imdb_id"], "tt0137523")
