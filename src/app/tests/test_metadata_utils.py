from django.test import SimpleTestCase

from app.metadata_utils import apply_provider_episode_count
from app.models import Item, MediaTypes


class ProviderEpisodeCountTests(SimpleTestCase):
    """Validate normalization of provider episode totals."""

    def test_applies_provider_count_from_details(self):
        item = Item(media_type=MediaTypes.SEASON.value)

        changed = apply_provider_episode_count(
            item,
            {"details": {"episodes": "8"}},
        )

        self.assertEqual(changed, ["provider_episode_count"])
        self.assertEqual(item.provider_episode_count, 8)

    def test_does_not_apply_episode_count_to_movies(self):
        item = Item(media_type=MediaTypes.MOVIE.value)

        changed = apply_provider_episode_count(item, {"max_progress": 1})

        self.assertEqual(changed, [])
        self.assertIsNone(item.provider_episode_count)
