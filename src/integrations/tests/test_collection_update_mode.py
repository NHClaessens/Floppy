"""Tests for collection update mode in imports."""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import TV, CollectionEntry, Item, MediaTypes, Movie, Sources, Status
from integrations.models import PlexAccount
from integrations.tasks import update_collection_metadata_from_plex
from integrations.tasks._plex_collection import MAX_COLLECTION_SCAN_METADATA_FETCHES


class CollectionUpdateModeTest(TestCase):
    """Test collection update mode for Plex imports."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.plex_account = PlexAccount.objects.create(
            user=self.user,
            plex_token="test_token",
            plex_username="test",
            sections=[
                {
                    "id": "1",
                    "title": "Movies",
                    "type": "movie",
                    "uri": "http://plex.example.com",
                    "machine_identifier": "test_machine",
                }
            ],
        )

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

        # Create a tracked movie (mock metadata fetching to avoid API calls)
        with patch("app.providers.services.get_media_metadata") as mock_metadata:
            mock_metadata.return_value = {
                "title": "Test Movie",
                "image": "http://example.com/image.jpg",
                "max_progress": 1,
                "details": {"max_progress": 1},
            }
            self.movie = Movie.objects.create(
                user=self.user,
                item=self.item,
                status=Status.COMPLETED.value,
            )

        self.tv_item = Item.objects.create(
            media_id="254013",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Revival",
            image="http://example.com/image.jpg",
        )
        self.tv = TV.objects.create(
            user=self.user,
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
        )

    @patch("integrations.tasks._plex_collection.plex_api.list_resources")
    @patch("integrations.tasks._plex_collection.plex_api.fetch_history")
    @patch("integrations.tasks._plex_collection.plex_api.fetch_metadata")
    @patch("integrations.tasks._plex_collection.extract_collection_metadata_from_plex")
    def test_update_collection_metadata_from_plex(
        self,
        mock_extract,
        mock_fetch_metadata,
        mock_fetch_history,
        mock_list_resources,
    ):
        """Test collection metadata update from Plex update mode."""
        # Mock resources
        mock_list_resources.return_value = [
            {
                "machine_identifier": "test_machine",
                "connections": [{"uri": "http://plex.example.com"}],
            }
        ]

        # Mock history entries with matching item
        mock_fetch_history.return_value = (
            [
                {
                    "ratingKey": "12345",
                    "Guid": [{"id": "tmdb://1234"}],
                }
            ],
            "http://plex.example.com",
        )

        # Mock Plex metadata
        mock_plex_metadata = {
            "Media": [
                {
                    "videoResolution": "1080",
                    "videoCodec": "hevc",
                    "audioCodec": "dca",
                    "audioChannels": "6",
                }
            ]
        }
        mock_fetch_metadata.return_value = mock_plex_metadata

        # Mock collection metadata extraction
        mock_extract.return_value = {
            "resolution": "1080p",
            "hdr": "HDR10",
            "audio_codec": "DTS",
            "audio_channels": "5.1",
            "media_type": "digital",
        }

        # Call the task
        result = update_collection_metadata_from_plex(
            library="all",
            user_id=self.user.id,
        )

        # Verify result
        self.assertIn("updated", result)
        self.assertIn("errors", result)

        # If item was matched, verify collection entry was created/updated
        if result["updated"] > 0:
            entry = CollectionEntry.objects.get(user=self.user, item=self.item)
            self.assertEqual(entry.resolution, "1080p")
            self.assertEqual(entry.audio_codec, "DTS")

    @patch("integrations.tasks._plex_collection.plex_api.list_resources")
    @patch("integrations.tasks._plex_collection.plex_api.fetch_section_all_items")
    def test_update_collection_metadata_from_plex_filters_to_selected_libraries(
        self,
        mock_fetch_section_all_items,
        mock_list_resources,
    ):
        """A multi-library selection only scans the chosen sections."""
        self.plex_account.sections = [
            *self.plex_account.sections,
            {
                "id": "2",
                "title": "TV Shows",
                "type": "show",
                "uri": "http://plex.example.com",
                "machine_identifier": "test_machine",
            },
        ]
        self.plex_account.save(update_fields=["sections"])

        mock_list_resources.return_value = [
            {
                "machine_identifier": "test_machine",
                "connections": [{"uri": "http://plex.example.com"}],
            }
        ]
        mock_fetch_section_all_items.return_value = ([], 0)

        update_collection_metadata_from_plex(
            library=["test_machine::1"],
            user_id=self.user.id,
        )

        # Only the "Movies" section (id 1) is selected; the added "TV Shows"
        # section (id 2) must never be scanned.
        mock_fetch_section_all_items.assert_called_once()
        self.assertEqual(mock_fetch_section_all_items.call_args.args[2], "1")

    @patch(
        "integrations.tasks._plex_collection.update_collection_metadata_from_plex_webhook"
    )
    @patch("integrations.tasks._plex_collection.plex_api.fetch_metadata")
    @patch("integrations.tasks._plex_collection.plex_api.fetch_section_all_items")
    @patch("integrations.tasks._plex_collection.plex_api.list_resources")
    def test_matches_tv_show_with_only_bare_plex_guid_in_list_response(
        self,
        mock_list_resources,
        mock_fetch_section_all_items,
        mock_fetch_metadata,
        mock_webhook,
    ):
        """Regression test for issue #1172.

        The Plex library-list endpoint (`/library/sections/<id>/all`) often
        only exposes a bare "plex://..." guid per entry, not the full
        Guid[] array. The scan must fall back to fetching detailed
        per-item metadata to resolve a matchable TMDB/IMDB/TVDB id instead
        of silently giving up because a `plex_guid` entry made the
        extracted-ids dict look non-empty.
        """
        self.plex_account.sections = [
            {
                "id": "2",
                "title": "TV Shows",
                "type": "show",
                "uri": "http://plex.example.com",
                "machine_identifier": "test_machine",
            }
        ]
        self.plex_account.save(update_fields=["sections"])

        mock_list_resources.return_value = [
            {
                "machine_identifier": "test_machine",
                "connections": [{"uri": "http://plex.example.com"}],
            }
        ]

        # The list endpoint returns only the primary agent guid, no Guid[] array.
        mock_fetch_section_all_items.return_value = (
            [
                {
                    "ratingKey": "4706",
                    "guid": "plex://show/663e10be2ec7c36ca7b8a3b9",
                }
            ],
            1,
        )

        # The detailed per-item endpoint returns the full external id set.
        mock_fetch_metadata.return_value = {
            "Guid": [
                {"id": "imdb://tt13951052"},
                {"id": "tmdb://254013"},
                {"id": "tvdb://449906"},
            ]
        }
        mock_webhook.return_value = 1

        update_collection_metadata_from_plex(
            library="all",
            user_id=self.user.id,
        )

        mock_fetch_metadata.assert_called_once_with(
            self.plex_account.plex_token, "http://plex.example.com", "4706"
        )
        mock_webhook.assert_called_once()
        self.assertEqual(mock_webhook.call_args.kwargs["item_id"], self.tv_item.id)
        self.assertEqual(mock_webhook.call_args.kwargs["rating_key"], "4706")

    @patch(
        "integrations.tasks._plex_collection.update_collection_metadata_from_plex_webhook"
    )
    @patch("integrations.tasks._plex_collection.plex_api.fetch_metadata")
    @patch("integrations.tasks._plex_collection.plex_api.fetch_section_all_items")
    @patch("integrations.tasks._plex_collection.plex_api.list_resources")
    def test_bounds_per_entry_metadata_fallback_calls(
        self,
        mock_list_resources,
        mock_fetch_section_all_items,
        mock_fetch_metadata,
        mock_webhook,
    ):
        """A section full of unresolvable bare-guid entries must not turn
        into an unbounded number of blocking per-item metadata fetches.
        """
        self.plex_account.sections = [
            {
                "id": "2",
                "title": "TV Shows",
                "type": "show",
                "uri": "http://plex.example.com",
                "machine_identifier": "test_machine",
            }
        ]
        self.plex_account.save(update_fields=["sections"])

        mock_list_resources.return_value = [
            {
                "machine_identifier": "test_machine",
                "connections": [{"uri": "http://plex.example.com"}],
            }
        ]

        entry_count = MAX_COLLECTION_SCAN_METADATA_FETCHES + 100
        entries = [
            {"ratingKey": str(i), "guid": f"plex://show/{i}"}
            for i in range(entry_count)
        ]
        mock_fetch_section_all_items.return_value = (entries, entry_count)

        # No entry ever resolves to a matchable external id, even after the
        # detailed metadata fallback fetch.
        mock_fetch_metadata.return_value = None

        update_collection_metadata_from_plex(
            library="all",
            user_id=self.user.id,
        )

        self.assertEqual(
            mock_fetch_metadata.call_count, MAX_COLLECTION_SCAN_METADATA_FETCHES
        )
        mock_webhook.assert_not_called()
