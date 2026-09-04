"""The Stremio settings page should say how much of a catalog can be published.

project_catalog() already counts the items it drops for want of an IMDb ID and
only logs the number, so a user whose movies were all silently unpublishable had
no way to tell that from an empty list (issue #1066).
"""

from django.test import TestCase

from app.models import Item, MediaTypes, Sources
from integrations import stremio_catalog
from lists.models import CustomList, CustomListItem
from users.models import User


class CatalogReadinessTests(TestCase):
    """Counts come from the same local_imdb_id() rule the projection uses."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="reader",
            email="reader@example.com",
            password="pw",  # test-only credential
        )
        self.movies = CustomList.objects.create(name="Movies", owner=self.user)

    def add_movie(self, media_id, provider_external_ids=None):
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=f"Movie {media_id}",
            provider_external_ids=provider_external_ids or {},
        )
        CustomListItem.objects.create(custom_list=self.movies, item=item)
        return item

    def test_counts_split_publishable_from_unresolved(self):
        self.add_movie(550, {"imdb_id": "tt0137523"})
        self.add_movie(551)
        self.add_movie(552, {"tmdb_id": "552"})

        readiness = stremio_catalog.catalog_readiness(self.user)

        self.assertEqual(len(readiness), 1)
        row = readiness[0]
        self.assertEqual(row["noun"], "movies")
        self.assertEqual(row["list_name"], "Movies")
        self.assertEqual(row["total"], 3)
        self.assertEqual(row["publishable"], 1)
        self.assertEqual(row["unresolved"], 2)

    def test_a_fully_resolved_list_reports_no_unresolved(self):
        self.add_movie(550, {"imdb_id": "tt0137523"})

        row = stremio_catalog.catalog_readiness(self.user)[0]

        self.assertEqual(row["unresolved"], 0)
        self.assertEqual(row["publishable"], 1)

    def test_empty_lists_are_omitted(self):
        self.assertEqual(stremio_catalog.catalog_readiness(self.user), [])

    def test_counts_agree_with_the_projection(self):
        """The status line must not claim more than the catalog would serve."""
        self.add_movie(550, {"imdb_id": "tt0137523"})
        self.add_movie(551)

        spec = next(
            spec
            for spec in stremio_catalog.CATALOG_SPECS
            if spec.stremio_type == "movie"
        )
        metas, unresolved_count = stremio_catalog.project_catalog(self.user, spec, 0)
        row = stremio_catalog.catalog_readiness(self.user)[0]

        self.assertEqual(row["publishable"], len(metas))
        self.assertEqual(row["unresolved"], unresolved_count)
