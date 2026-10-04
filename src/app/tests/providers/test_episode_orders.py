"""Provider order discovery, identity and completeness regression tests."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase

from app.providers import episode_orders


class EpisodeOrderProviderTests(SimpleTestCase):
    """Provider catalogues must retain identity when coordinates change."""

    def setUp(self):
        cache.clear()
        self.credentials = patch.object(
            episode_orders.credentials, "cache_suffix", return_value="credential-a",
        )
        self.credentials.start()
        self.addCleanup(self.credentials.stop)
        self.addCleanup(cache.clear)

    @patch.object(episode_orders, "_request")
    def test_tmdb_group_uses_group_coordinates_and_stable_episode_id(self, request):
        request.side_effect = [
            {"results": [{"id": "dvd-id", "name": "DVD release"}]},
            {"groups": [{"order": 1, "episodes": [{
                "id": 777, "order": 0, "season_number": 5,
                "episode_number": 17, "name": "Correct episode",
                "still_path": "/correct.jpg", "runtime": 45,
            }]}]},
        ]
        result = episode_orders.fetch_order("tmdb", "123", "group:dvd-id")
        episode = result["episodes"][0]
        self.assertEqual(episode["provider_episode_id"], "777")
        self.assertEqual((episode["season_number"], episode["episode_number"]), (1, 1))
        self.assertEqual(episode["title"], "Correct episode")
        self.assertIn("correct.jpg", episode["image"])

    @patch.object(episode_orders, "_request")
    def test_tvdb_discovers_types_and_keeps_default_distinct(self, request):
        request.return_value = {"data": {"seasonTypes": [
            {"type": "official", "name": "Aired Order"},
            {"type": "production", "name": "Production"},
        ]}}
        result = episode_orders.list_orders("tvdb", "123")
        self.assertEqual([row["key"] for row in result], ["default", "official", "production"])

    @patch.object(episode_orders, "_request")
    def test_tvdb_paginates_and_preserves_order_coordinates(self, request):
        request.side_effect = [
            {"data": {"seasonTypes": [{"type": "dvd", "name": "DVD"}]}},
            {"data": {"episodes": [{"id": 1, "seasonNumber": 2, "number": 3}]},
             "links": {"next": "ignored-provider-url"}},
            {"data": {"episodes": [{"id": 2, "seasonNumber": 2, "number": 4}]}},
        ]
        result = episode_orders.fetch_order("tvdb", "123", "dvd", language="fr")
        self.assertEqual(len(result["episodes"]), 2)
        self.assertEqual(request.call_args.args[1], "series/123/episodes/dvd/fra")
        self.assertEqual(request.call_args.args[-1], {"page": 1})

    @patch.object(episode_orders, "MAX_PAGES", 1)
    @patch.object(episode_orders, "_request")
    def test_partial_catalogue_is_not_cached(self, request):
        # An explicitly empty type list is valid; default still exists.
        request.side_effect = [
            {"data": {"seasonTypes": [], "seasons": []}},
            {"data": {"episodes": [{"id": 1, "seasonNumber": 1, "number": 1}]},
             "links": {"next": "next"}},
        ]
        with self.assertRaisesRegex(ValueError, "pagination limit"):
            episode_orders.fetch_order("tvdb", "123", "default")
        request.side_effect = None
        request.return_value = {"data": {"episodes": [
            {"id": 1, "seasonNumber": 1, "number": 1},
        ]}}
        result = episode_orders.fetch_order("tvdb", "123", "default")
        self.assertEqual(len(result["episodes"]), 1)
        self.assertEqual(request.call_count, 3)

    @patch.object(episode_orders, "_request")
    def test_unknown_group_is_rejected_before_fetch(self, request):
        request.return_value = {"results": []}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            episode_orders.fetch_order("tmdb", "123", "group:other-series")
        request.assert_called_once()

    @patch.object(episode_orders, "_request")
    def test_cache_separates_language_and_credentials(self, request):
        request.return_value = {"results": []}
        episode_orders.list_orders("tmdb", "123", language="en")
        episode_orders.list_orders("tmdb", "123", language="en")
        episode_orders.list_orders("tmdb", "123", language="fr")
        with patch.object(episode_orders.credentials, "cache_suffix", return_value="b"):
            episode_orders.list_orders("tmdb", "123", language="en")
        self.assertEqual(request.call_count, 3)

    @patch.object(episode_orders, "_request")
    def test_repeated_tvdb_page_is_rejected(self, request):
        row = {"id": 1, "seasonNumber": 1, "number": 1}
        request.side_effect = [
            {"data": {"seasonTypes": [], "seasons": []}},
            {"data": {"episodes": [row]}, "links": {"next": "next"}},
            {"data": {"episodes": [row]}},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            episode_orders.fetch_order("tvdb", "123", "default")
