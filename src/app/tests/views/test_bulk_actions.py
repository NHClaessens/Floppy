from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    CollectionEntry,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)


class BulkStatusViewTests(TestCase):
    """Test item-based bulk status updates."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bulk-status-user",
            password="test-password",
        )
        self.client.login(username="bulk-status-user", password="test-password")
        self.existing_item = Item.objects.create(
            media_id="existing-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Existing Movie",
        )
        self.missing_tracker_item = Item.objects.create(
            media_id="missing-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Missing Tracker Movie",
        )
        self.episode_item = Item.objects.create(
            media_id="episode-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode",
        )
        Movie.objects.create(
            item=self.existing_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

    def _post(self, item_ids, status=Status.PAUSED.value):
        # A plain dict with a list value, not a QueryDict: the test client's
        # multipart encoder iterates data.items(), and QueryDict.items()
        # yields only the last value per key, so every id but the last would
        # be dropped before the request was sent.
        payload = {
            "item_ids": [str(item_id) for item_id in item_ids],
            "status": status,
        }
        return self.client.post(reverse("bulk_status_update"), payload)

    def test_updates_existing_creates_missing_and_skips_episode(self):
        response = self._post(
            [self.existing_item.id, self.missing_tracker_item.id, self.episode_item.id],
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 1)
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertEqual(
            Movie.objects.get(item=self.existing_item, user=self.user).status,
            Status.PAUSED.value,
        )
        self.assertTrue(
            Movie.objects.filter(
                item=self.missing_tracker_item,
                user=self.user,
                status=Status.PAUSED.value,
            ).exists(),
        )
        self.assertFalse(Episode.objects.filter(item=self.episode_item).exists())

    def test_rejects_invalid_status(self):
        response = self._post([self.existing_item.id], status="invalid")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_reports_unknown_items_as_skipped(self):
        response = self._post([self.existing_item.id, 999999])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 1)
        self.assertEqual(response.json()["skipped"], 1)


class BulkCollectionViewTests(TestCase):
    """Test idempotent item-based collection quick-adds."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bulk-collection-user",
            password="test-password",
        )
        self.client.login(
            username="bulk-collection-user",
            password="test-password",
        )
        self.movie = Item.objects.create(
            media_id="collection-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Collection Movie",
        )
        self.show = Item.objects.create(
            media_id="collection-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Collection Show",
        )
        for episode_number in (1, 2):
            Item.objects.create(
                media_id=self.show.media_id,
                source=self.show.source,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=episode_number,
                title=f"Episode {episode_number}",
            )

    def _post(self, item_ids):
        # A plain dict with a list value, not a QueryDict: the test client's
        # multipart encoder iterates data.items(), and QueryDict.items()
        # yields only the last value per key, so every id but the last would
        # be dropped before the request was sent.
        payload = {"item_ids": [str(item_id) for item_id in item_ids]}
        return self.client.post(reverse("bulk_collection_quick_add"), payload)

    def test_creates_ordinary_and_expanded_entries_idempotently(self):
        response = self._post([self.movie.id, self.show.id, 999999])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 3)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertEqual(CollectionEntry.objects.filter(user=self.user).count(), 3)

        response = self._post([self.movie.id, self.show.id, 999999])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 0)
        self.assertEqual(response.json()["already_present"], 3)
        self.assertEqual(response.json()["skipped"], 1)
