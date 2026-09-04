from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings

from app import fork_services_podcast
from app.models import PodcastEpisode, PodcastShow

SHOW_METADATA = {
    "title": "Voicemail Dump Truck",
    "description": "A show.",
    "website_url": "https://www.spreaker.com/show/voicemail-dump-truck",
}


def _feed_episode(**overrides):
    episode = {
        "title": "Episode One",
        "published": datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        "guid": "guid-one",
        "audio_url": "https://example.com/one.mp3",
        "website_url": "https://www.spreaker.com/episode/one",
    }
    episode.update(overrides)
    return episode


# Episode matching falls back to title + publication date, and Django resolves
# a date in the active timezone, so the fixtures below only line up under a
# fixed one.
@override_settings(TIME_ZONE="UTC")
class RefreshShowFromRssBackfillTests(TestCase):
    """Re-reading a feed repairs rows created before website links existed."""

    def setUp(self):
        self.show = PodcastShow.objects.create(
            podcast_uuid="gp_backfill",
            title="Voicemail Dump Truck",
            rss_feed_url="https://example.com/feed.xml",
        )

    def _refresh(self, metadata=None, episodes=None):
        payload = (
            SHOW_METADATA if metadata is None else metadata,
            [_feed_episode()] if episodes is None else episodes,
        )
        with patch(
            "integrations.podcast_rss.fetch_feed_from_rss",
            return_value=payload,
        ) as mock_feed:
            fork_services_podcast.refresh_show_from_rss(self.show)
        return mock_feed

    def test_backfills_website_url_on_an_existing_show_and_episode(self):
        episode = PodcastEpisode.objects.create(
            show=self.show,
            episode_uuid="guid-one",
            title="Episode One",
            published=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        )

        self._refresh()

        self.show.refresh_from_db()
        episode.refresh_from_db()
        self.assertEqual(self.show.website_url, SHOW_METADATA["website_url"])
        self.assertEqual(episode.website_url, "https://www.spreaker.com/episode/one")
        self.assertEqual(PodcastEpisode.objects.filter(show=self.show).count(), 1)

    def test_backfills_an_episode_stored_under_a_provider_id(self):
        """A gPodder/Pocket Casts episode is keyed by the provider's id, not the GUID."""
        episode = PodcastEpisode.objects.create(
            show=self.show,
            episode_uuid="gp_not_the_feed_guid",
            title="  episode one  ",
            published=datetime(2026, 1, 2, 23, 30, tzinfo=UTC),
        )

        self._refresh(
            episodes=[
                _feed_episode(
                    published=datetime(2026, 1, 2, 1, 0, tzinfo=UTC),
                ),
            ],
        )

        episode.refresh_from_db()
        self.assertEqual(episode.website_url, "https://www.spreaker.com/episode/one")
        self.assertEqual(PodcastEpisode.objects.filter(show=self.show).count(), 1)

    def test_creates_missing_episodes_with_their_website_url(self):
        self._refresh()

        episode = PodcastEpisode.objects.get(show=self.show, episode_uuid="guid-one")
        self.assertEqual(episode.website_url, "https://www.spreaker.com/episode/one")

    def test_a_settled_feed_writes_nothing_on_a_second_pass(self):
        self._refresh()

        with self.assertNumQueries(1):
            # Just the SELECT that indexes the show's episodes: the metadata
            # pass writes nothing when the feed matches, and no episode row
            # needs updating. A settled feed must not cost a write on every
            # detail page view.
            self._refresh()

    def test_a_feed_without_links_leaves_stored_values_alone(self):
        episode = PodcastEpisode.objects.create(
            show=self.show,
            episode_uuid="guid-one",
            title="Episode One",
            published=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
            website_url="https://example.com/kept",
        )
        self.show.website_url = "https://example.com/kept-show"
        self.show.save(update_fields=["website_url"])

        self._refresh(
            metadata={"title": "Voicemail Dump Truck"},
            episodes=[_feed_episode(website_url="")],
        )

        self.show.refresh_from_db()
        episode.refresh_from_db()
        self.assertEqual(self.show.website_url, "https://example.com/kept-show")
        self.assertEqual(episode.website_url, "https://example.com/kept")

    def test_reads_the_feed_document_only_once(self):
        """The detail page runs this on every view.

        Reading the channel and the items through separate entry points would
        fetch the same document twice per page load.
        """
        mock_feed = self._refresh()

        mock_feed.assert_called_once_with(self.show.rss_feed_url, limit=None)

    def test_an_unreachable_feed_is_not_fatal(self):
        with patch(
            "integrations.podcast_rss.fetch_feed_from_rss",
            side_effect=OSError("boom"),
        ):
            fork_services_podcast.refresh_show_from_rss(self.show)

        self.show.refresh_from_db()
        self.assertEqual(self.show.website_url, "")
