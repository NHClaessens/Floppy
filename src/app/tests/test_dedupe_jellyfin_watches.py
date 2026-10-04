"""Tests for the dedupe_jellyfin_watches repair command."""

from datetime import UTC, datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    MoviePlay,
    Season,
    Sources,
    Status,
)


class DedupeJellyfinWatchesTests(TestCase):
    """The command collapses near-duplicate webhook/import watch entries."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="watcher",
            password="12345",
        )

    def _run(self, *args):
        out = StringIO()
        call_command("dedupe_jellyfin_watches", *args, stdout=out)
        return out.getvalue()

    def _episode_setup(self, source=Sources.TVDB.value):
        tv_item = Item.objects.create(
            media_id="series-1",
            source=source,
            media_type=MediaTypes.TV.value,
            title="Show",
        )
        tv = TV.objects.create(
            user=self.user,
            item=tv_item,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="series-1",
            source=source,
            media_type=MediaTypes.SEASON.value,
            title="Season 1",
            season_number=1,
        )
        season = Season.objects.create(
            user=self.user,
            item=season_item,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_id="series-1",
            source=source,
            media_type=MediaTypes.EPISODE.value,
            title="Episode",
            season_number=1,
            episode_number=1,
        )
        return season, episode_item

    def test_dry_run_reports_without_deleting(self):
        season, episode_item = self._episode_setup()
        webhook_time = datetime(2024, 1, 2, 3, 34, 0, tzinfo=UTC)
        import_time = webhook_time - timedelta(minutes=30)
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=webhook_time,
            watch_operation_id=None,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=import_time,
            watch_operation_id="11111111-1111-1111-1111-111111111111",
        )

        output = self._run()

        self.assertIn("DRY RUN", output)
        self.assertEqual(Episode.objects.count(), 2)
        self.assertIn("to remove", output)

    def test_apply_collapses_episode_duplicates_keeping_provenance(self):
        season, episode_item = self._episode_setup()
        webhook_time = datetime(2024, 1, 2, 3, 34, 0, tzinfo=UTC)
        import_time = webhook_time - timedelta(minutes=30)
        webhook_episode = Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=webhook_time,
        )
        import_episode = Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=import_time,
            watch_operation_id="11111111-1111-1111-1111-111111111111",
        )

        self._run("--apply")

        self.assertEqual(Episode.objects.count(), 1)
        remaining = Episode.objects.get()
        self.assertEqual(remaining.pk, import_episode.pk)
        self.assertFalse(Episode.objects.filter(pk=webhook_episode.pk).exists())

    def test_apply_leaves_genuine_rewatch_outside_window_alone(self):
        season, episode_item = self._episode_setup()
        first_watch = datetime(2024, 1, 2, 3, 34, 0, tzinfo=UTC)
        rewatch = first_watch + timedelta(days=30)
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=first_watch,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=rewatch,
        )

        self._run("--apply")

        self.assertEqual(Episode.objects.count(), 2)

    def test_apply_collapses_movie_play_duplicates(self):
        item = Item.objects.create(
            media_id="movie-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="A Movie",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        webhook_time = datetime(2024, 1, 2, 3, 34, 0, tzinfo=UTC)
        import_time = webhook_time - timedelta(minutes=20)
        webhook_play = MoviePlay.objects.create(movie=movie, end_date=webhook_time)
        import_play = MoviePlay.objects.create(
            movie=movie,
            end_date=import_time,
            external_id="jellyfin-playback-reporting:hash:abc",
        )

        self._run("--apply")

        self.assertEqual(MoviePlay.objects.filter(movie=movie).count(), 1)
        remaining = MoviePlay.objects.get(movie=movie)
        self.assertEqual(remaining.pk, import_play.pk)
        self.assertFalse(MoviePlay.objects.filter(pk=webhook_play.pk).exists())

    def test_username_filter_scopes_to_one_user(self):
        other_user = get_user_model().objects.create_user(
            username="other",
            password="12345",
        )
        season, episode_item = self._episode_setup()
        webhook_time = datetime(2024, 1, 2, 3, 34, 0, tzinfo=UTC)
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=webhook_time,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=webhook_time - timedelta(minutes=10),
        )

        self._run("--apply", "--username", other_user.username)

        self.assertEqual(Episode.objects.count(), 2)

    def test_apply_preserves_distinct_watches_across_a_transitive_chain(self):
        """A 00:00/00:50/01:40 chain under a 60-minute window must not
        collapse into one cluster: 00:00 and 01:40 are not duplicates of
        each other, only each is a duplicate of the row in between.
        """
        season, episode_item = self._episode_setup()
        episode_item.runtime_minutes = 60
        episode_item.save(update_fields=["runtime_minutes"])
        base = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=base,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=base + timedelta(minutes=50),
        )
        distinct_watch = Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=base + timedelta(minutes=100),
        )

        self._run("--apply")

        self.assertEqual(Episode.objects.count(), 2)
        self.assertTrue(Episode.objects.filter(pk=distinct_watch.pk).exists())

    def test_apply_syncs_movie_end_date_after_removing_the_play_it_pointed_to(self):
        """Movie.end_date must not keep naming a play that no longer exists."""
        item = Item.objects.create(
            media_id="movie-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="A Movie",
        )
        stop_time = datetime(2024, 1, 2, 3, 34, 0, tzinfo=UTC)
        start_time = stop_time - timedelta(minutes=20)
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            end_date=stop_time,
        )
        # The webhook's own watch, lazily copied into a provenance-free play
        # the first time Movie.watch() ran.
        MoviePlay.objects.create(movie=movie, end_date=stop_time)
        # The history import's play for the same watch.
        MoviePlay.objects.create(
            movie=movie,
            end_date=start_time,
            external_id="jellyfin-playback-reporting:hash:abc",
        )

        self._run("--apply")

        movie.refresh_from_db()
        self.assertEqual(MoviePlay.objects.filter(movie=movie).count(), 1)
        self.assertEqual(movie.end_date, start_time)

    def test_episode_grouping_respects_item_source(self):
        """Same numeric ids under different providers must not be merged."""
        tvdb_season, tvdb_episode_item = self._episode_setup(
            source=Sources.TVDB.value,
        )
        tmdb_season, tmdb_episode_item = self._episode_setup(
            source=Sources.TMDB.value,
        )
        watch_time = datetime(2024, 1, 2, 3, 34, 0, tzinfo=UTC)
        Episode.objects.create(
            item=tvdb_episode_item,
            related_season=tvdb_season,
            end_date=watch_time,
        )
        Episode.objects.create(
            item=tmdb_episode_item,
            related_season=tmdb_season,
            end_date=watch_time + timedelta(minutes=5),
        )

        self._run("--apply")

        self.assertEqual(Episode.objects.count(), 2)
