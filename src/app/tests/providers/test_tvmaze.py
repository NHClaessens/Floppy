from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from app.providers import services, tvmaze


class TVMazeProviderTests(TestCase):
    """Tests for TVMaze external-ID resolution."""

    def setUp(self):
        cache.clear()

    def test_external_ids_returns_none_for_missing_id(self):
        self.assertIsNone(tvmaze.external_ids(None))
        self.assertIsNone(tvmaze.external_ids(""))

    @patch("app.providers.tvmaze.services.api_request")
    def test_external_ids_returns_mapped_ids(self, mock_api_request):
        mock_api_request.return_value = {
            "id": 38390,
            "externals": {"thetvdb": 352408, "imdb": "tt9054364", "tvrage": None},
        }

        result = tvmaze.external_ids("38390")

        self.assertEqual(result, {"tvdb_id": "352408", "imdb_id": "tt9054364"})
        mock_api_request.assert_called_once_with(
            "tvmaze", "GET", f"{tvmaze.base_url}/shows/38390",
        )

    @patch("app.providers.tvmaze.services.api_request")
    def test_external_ids_caches_result(self, mock_api_request):
        mock_api_request.return_value = {
            "id": 38390,
            "externals": {"thetvdb": 352408, "imdb": None},
        }

        first = tvmaze.external_ids("38390")
        second = tvmaze.external_ids("38390")

        self.assertEqual(first, second)
        mock_api_request.assert_called_once()

    @patch("app.providers.tvmaze.services.api_request")
    def test_external_ids_returns_none_when_no_externals(self, mock_api_request):
        mock_api_request.return_value = {"id": 38390, "externals": {}}

        self.assertIsNone(tvmaze.external_ids("38390"))

    @patch("app.providers.tvmaze.services.api_request")
    def test_external_ids_returns_none_on_provider_error(self, mock_api_request):
        mock_api_request.side_effect = services.ProviderAPIError(
            "tvmaze", RuntimeError("not found"),
        )

        self.assertIsNone(tvmaze.external_ids("does-not-exist"))
