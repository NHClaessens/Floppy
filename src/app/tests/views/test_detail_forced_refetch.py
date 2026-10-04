"""A field the provider never returns must not force a live refetch every visit."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from app.models import Item, MediaTypes, Sources


class DetailForcedRefetchTests(TestCase):
    """The secondary fragment refetches a missing original title once a day."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="refetch")
        cls.user.title_display_preference = "original"
        cls.user.save()
        cls.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="No Original Title",
        )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    @patch("integrations.tasks.fetch_collection_metadata_for_item.delay")
    @patch("app.views.credits.sync_item_credits_from_metadata")
    @patch("app.views.metadata_utils.apply_item_metadata", return_value=[])
    @patch("app.providers.services.get_media_metadata")
    def test_missing_original_title_refetches_once(self, mock_metadata, *_mocks):
        mock_metadata.return_value = {
            "media_id": "238",
            "title": "No Original Title",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "max_progress": 1,
            "details": {},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        url = reverse(
            "media_details",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "media_id": "238",
                "title": "no-original-title",
            },
        )

        self.client.get(url, {"fragment": "secondary"})
        calls_after_first_visit = mock_metadata.call_count
        self.client.get(url, {"fragment": "secondary"})

        # The second visit makes only the fragment's normal lookup, not the
        # extra forced refetch.
        self.assertEqual(
            mock_metadata.call_count - calls_after_first_visit,
            calls_after_first_visit - 1,
        )
