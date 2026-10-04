"""A repeat provider-link upsert with unchanged data must not write."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from app.models import Item, MediaTypes, Sources
from app.services import metadata_resolution


class ProviderLinkWriteTests(TestCase):
    """Detail pages call the upsert on GET; unchanged links stay read-only."""

    def test_unchanged_links_issue_no_writes(self):
        item = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Linked",
        )
        metadata = {
            "media_id": "603",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "external_ids": {"imdb_id": "tt0133093"},
        }
        metadata_resolution.upsert_provider_links(item, metadata)
        item.refresh_from_db()

        with CaptureQueriesContext(connection) as captured:
            metadata_resolution.upsert_provider_links(item, metadata)

        writes = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
