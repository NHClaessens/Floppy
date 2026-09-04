from datetime import UTC, datetime
from unittest.mock import call, patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from app import tasks_podcast
from app.interactive_requests import INTERACTIVE_REQUEST_CACHE_KEY
from app.models import BackfillReconcileState, PodcastEpisode, PodcastShow


class ReconcilePodcastWebsiteBackfillTests(TestCase):
    """The one-shot sweep walks every show with a feed, then stops for good."""

    def setUp(self):
        # ensure_* checks interactive_request_active() before the state gate,
        # and that flag lives in the cache, so a test elsewhere that sets it
        # and doesn't clear it makes this class fail on ordering alone. Same
        # guard MetadataBackfillTaskTests uses.
        cache.delete(INTERACTIVE_REQUEST_CACHE_KEY)
        BackfillReconcileState.objects.filter(
            key=tasks_podcast.RECONCILE_KEY,
        ).delete()

    def tearDown(self):
        cache.delete(INTERACTIVE_REQUEST_CACHE_KEY)

    def _show(self, uuid, *, rss_feed_url="https://example.com/feed.xml"):
        return PodcastShow.objects.create(
            podcast_uuid=uuid,
            title=uuid,
            rss_feed_url=rss_feed_url,
        )

    @patch("app.tasks_podcast.backfill_podcast_show_websites.apply_async")
    def test_queues_shows_with_a_feed_in_staggered_chunks(self, mock_apply_async):
        first = self._show("show-one")
        second = self._show("show-two")
        self._show("show-without-a-feed", rss_feed_url="")

        result = tasks_podcast.reconcile_podcast_website_backfill(batch_size=1)

        self.assertEqual(
            mock_apply_async.call_args_list[:2],
            [
                call(
                    args=[[first.id]],
                    countdown=10,
                    priority=tasks_podcast.BACKGROUND_TASK_PRIORITY,
                ),
                call(
                    args=[[second.id]],
                    countdown=40,
                    priority=tasks_podcast.BACKGROUND_TASK_PRIORITY,
                ),
            ],
        )
        self.assertTrue(result["complete"])

    @patch("app.tasks_podcast.backfill_podcast_show_websites.apply_async")
    def test_marks_complete_once_the_cursor_runs_off_the_end(self, _mock_apply_async):
        self._show("show-one")

        tasks_podcast.reconcile_podcast_website_backfill(batch_size=10)

        state = BackfillReconcileState.objects.get(key=tasks_podcast.RECONCILE_KEY)
        self.assertIsNotNone(state.completed_at)

    @patch("app.tasks_podcast.backfill_podcast_show_websites.apply_async")
    def test_resumes_from_the_stored_cursor(self, mock_apply_async):
        first = self._show("show-one")
        second = self._show("show-two")
        BackfillReconcileState.objects.create(
            key=tasks_podcast.RECONCILE_KEY,
            strategy_version=tasks_podcast.PODCAST_WEBSITE_BACKFILL_VERSION,
            last_cursor_item_id=first.id,
        )

        tasks_podcast.reconcile_podcast_website_backfill(batch_size=10)

        queued_ids = mock_apply_async.call_args_list[0].kwargs["args"][0]
        self.assertEqual(queued_ids, [second.id])

    @patch("app.tasks_podcast.reconcile_podcast_website_backfill")
    def test_ensure_skips_a_completed_sweep(self, mock_reconcile):
        BackfillReconcileState.objects.create(
            key=tasks_podcast.RECONCILE_KEY,
            strategy_version=tasks_podcast.PODCAST_WEBSITE_BACKFILL_VERSION,
            completed_at=timezone.now(),
        )

        result = tasks_podcast.ensure_podcast_website_backfill_reconcile()

        mock_reconcile.assert_not_called()
        self.assertEqual(result, {"skipped": True, "reason": "not_due"})


class BackfillPodcastShowWebsitesTests(TestCase):
    """The worker task applies feed links and survives a dead feed."""

    def test_applies_show_and_episode_links(self):
        show = PodcastShow.objects.create(
            podcast_uuid="gp_backfill_task",
            title="Example Show",
            rss_feed_url="https://example.com/feed.xml",
        )
        published = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        episode = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="guid-one",
            title="Episode One",
            published=published,
        )

        with patch(
            "integrations.podcast_rss.fetch_feed_from_rss",
            return_value=(
                {"website_url": "https://example.com/show"},
                [
                    {
                        "title": "Episode One",
                        "published": published,
                        "guid": "guid-one",
                        "website_url": "https://example.com/show/one",
                    },
                ],
            ),
        ):
            result = tasks_podcast.backfill_podcast_show_websites([show.id])

        show.refresh_from_db()
        episode.refresh_from_db()
        self.assertEqual(result, {"processed": 1})
        self.assertEqual(show.website_url, "https://example.com/show")
        self.assertEqual(episode.website_url, "https://example.com/show/one")

    @patch(
        "app.fork_services_podcast.refresh_show_from_rss",
        side_effect=OSError("boom"),
    )
    def test_one_dead_feed_does_not_stop_the_batch(self, _mock_refresh):
        show = PodcastShow.objects.create(
            podcast_uuid="gp_dead_feed",
            title="Dead Feed",
            rss_feed_url="https://example.com/gone.xml",
        )

        result = tasks_podcast.backfill_podcast_show_websites([show.id])

        self.assertEqual(result, {"processed": 0})
