from unittest.mock import Mock, patch

from django.test import TestCase

from integrations import plex as plex_api


class FetchSectionAllItemsTests(TestCase):
    """fetch_section_all_items() must request full external-id Guids in bulk."""

    def _response(self, entries, total=None):
        response = Mock()
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = {
            "MediaContainer": {
                "Metadata": entries,
                "totalSize": total if total is not None else len(entries),
            },
        }
        return response

    @patch("integrations.plex.requests.get")
    def test_requests_include_guids_in_bulk(self, mock_get):
        """The list endpoint must ask for includeGuids so the Guid[] array
        comes back per item, avoiding a per-item detail fetch fallback.
        """
        mock_get.return_value = self._response(
            [{"ratingKey": "1", "Guid": [{"id": "tmdb://254013"}]}]
        )

        entries, total = plex_api.fetch_section_all_items(
            "token", "http://plex.example.com", "1"
        )

        self.assertEqual(total, 1)
        self.assertEqual(entries[0]["Guid"], [{"id": "tmdb://254013"}])
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["params"]["includeGuids"], 1)
