"""A date-filtered history request must page the day index, not rebuild it.

start_date/end_date are whole-day bounds, so a date range only ever drops
whole days from the history. Routing such a request through the builder made
its cost the size of the matching history rather than the size of the page it
returns -- 6,454 entries built to emit a 56 KiB response.
"""

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from app import history_cache, history_cache_reader
from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)

DAYS = 8
EPISODES_PER_DAY = 3


class HistoryDateWindowTests(TestCase):
    """The indexed window and the builder must agree on a date range."""

    @classmethod
    def setUpTestData(cls):
        cls.credentials = {"username": "history-window", "password": "12345"}
        cls.user = get_user_model().objects.create_user(**cls.credentials)
        tv_item = Item.objects.create(
            media_id="window-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Window Show",
            image="http://example.com/show.jpg",
            genres=["Drama"],
        )
        tv = TV.objects.create(
            item=tv_item, user=cls.user, status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="window-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Window Show Season 1",
        )
        season = Season.objects.create(
            item=season_item,
            user=cls.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        cls.today = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        number = 0
        for day in range(DAYS):
            for _ in range(EPISODES_PER_DAY):
                number += 1
                episode_item = Item.objects.create(
                    media_id="window-show",
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    season_number=1,
                    episode_number=number,
                    title=f"Window Episode {number}",
                    runtime_minutes=30,
                )
                Episode.objects.create(
                    item=episode_item,
                    related_season=season,
                    end_date=cls.today - timedelta(days=day),
                )
        # A second media type, so a type filter has something to exclude.
        movie_item = Item.objects.create(
            media_id="window-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Window Movie",
            runtime_minutes=90,
        )
        Movie.objects.create(
            item=movie_item,
            user=cls.user,
            status=Status.COMPLETED.value,
            end_date=cls.today - timedelta(days=2),
        )

    def setUp(self):
        """Every case starts cold, so nothing is answered from a stale index."""
        cache.clear()

    def _range(self):
        start = (self.today - timedelta(days=5)).date().isoformat()
        end = (self.today - timedelta(days=1)).date().isoformat()
        return {"start_date": start, "end_date": end}

    def _comparable(self, days):
        return [
            (
                day["date"],
                day["total_minutes"],
                [entry["entry_key"] for entry in day["entries"]],
            )
            for day in days
        ]

    def test_the_window_matches_the_builder_over_the_same_range(self):
        """Same days, same totals, same entries, in the same order."""
        date_filters = self._range()
        built = history_cache.get_history_days(
            self.user, date_filters=date_filters,
        )
        cache.clear()
        windowed, total_days = history_cache_reader.get_cached_history_window(
            self.user,
            limit=len(built) or 1,
            offset=0,
            date_filters=date_filters,
        )

        self.assertEqual(self._comparable(windowed), self._comparable(built))
        self.assertEqual(total_days, len(built))

    def test_the_range_bounds_are_inclusive(self):
        """A day named by start_date or end_date stays in the result."""
        date_filters = self._range()
        windowed, _ = history_cache_reader.get_cached_history_window(
            self.user, limit=50, offset=0, date_filters=date_filters,
        )

        dates = [day["date"].isoformat() for day in windowed]
        self.assertIn(date_filters["start_date"], dates)
        self.assertIn(date_filters["end_date"], dates)
        self.assertEqual(len(dates), 5)

    def test_a_type_filter_still_applies_inside_the_range(self):
        """Combining the two filters must not widen either one."""
        date_filters = {"start_date": (self.today - timedelta(days=2)).date().isoformat()}
        windowed, _ = history_cache_reader.get_cached_history_window(
            self.user,
            limit=50,
            offset=0,
            filters={"media_type": MediaTypes.TV.value},
            date_filters=date_filters,
        )

        media_types = {
            entry["media_type"] for day in windowed for entry in day["entries"]
        }
        self.assertEqual(media_types, {MediaTypes.EPISODE.value})

    def test_the_api_page_does_not_scale_with_the_history(self):
        """Adding days outside the page must not add queries or entries."""
        date_filters = self._range()
        params = {
            "media_type": MediaTypes.TV.value,
            "limit": 2,
            **date_filters,
        }
        url = reverse("api_history")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.user.token}"}

        with CaptureQueriesContext(connection) as before:
            first = self.client.get(url, params, **auth)

        self.assertEqual(first.status_code, HTTPStatus.OK)
        payload = first.json()
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(payload["pagination"]["total"], 5)

        season = Season.objects.get(user=self.user)
        number = DAYS * EPISODES_PER_DAY
        for day in range(DAYS, DAYS * 10):
            for _ in range(EPISODES_PER_DAY):
                number += 1
                episode_item = Item.objects.create(
                    media_id="window-show",
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    season_number=1,
                    episode_number=number,
                    title=f"Window Episode {number}",
                    runtime_minutes=30,
                )
                Episode.objects.create(
                    item=episode_item,
                    related_season=season,
                    end_date=self.today - timedelta(days=day),
                )
        cache.clear()

        with CaptureQueriesContext(connection) as after:
            second = self.client.get(url, params, **auth)

        self.assertEqual(second.json(), payload)
        self.assertLessEqual(
            len(after.captured_queries),
            len(before.captured_queries),
            "a 10x history made the same page cost more queries",
        )
