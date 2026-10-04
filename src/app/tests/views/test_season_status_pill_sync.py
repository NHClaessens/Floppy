from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status

METADATA_PATH = "app.providers.services.get_media_metadata"

SEASON_METADATA = {
    "episodes": [{"episode_number": 1}, {"episode_number": 2}],
    "max_progress": 2,
    "image": "s.jpg",
    "season/1": {"episodes": [{"episode_number": 1}, {"episode_number": 2}]},
    # An ended show, so finishing its only season finishes the show.
    "details": {"status": "Ended"},
}


@patch(METADATA_PATH, return_value=SEASON_METADATA)
class SeasonStatusPillSyncTests(TestCase):
    """The status pill stays in sync when the season's status changes indirectly.

    E.g. completing on the last episode, or reopening when a rewatch
    starts — rather than only from editing the season directly.
    """

    def setUp(self):
        """Track a two-episode season, one episode already watched."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        with patch(METADATA_PATH, return_value=SEASON_METADATA):
            tv_item = Item.objects.create(
                media_id="123",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="Show",
            )
            self.tv = TV.objects.create(item=tv_item, user=self.user)
            season_item = Item.objects.create(
                media_id="123",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Show",
                season_number=1,
            )
            self.season = Season.objects.create(
                item=season_item,
                user=self.user,
                related_tv=self.tv,
                status=Status.IN_PROGRESS.value,
            )
            self.episode_items = [
                Item.objects.create(
                    media_id="123",
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    title="Show",
                    season_number=1,
                    episode_number=n,
                )
                for n in (1, 2)
            ]
            Episode.objects.create(
                item=self.episode_items[0],
                related_season=self.season,
                end_date=datetime(2023, 6, 1, tzinfo=UTC),
            )

    def test_watching_the_last_episode_refreshes_the_status_pill(
        self,
        _mock_metadata,
    ):
        """Completing the season from an episode row updates the status pill too.

        Without this, the page keeps showing "In progress" until reloaded —
        the episode-save response only OOB-swaps that one episode's own
        button and the season's progress counter, never the season's own
        status pill (`detail_track_action.html`).
        """
        response = self.client.post(
            reverse("episode_save"),
            data={
                "media_id": "123",
                "season_number": 1,
                "episode_number": 2,
                "source": Sources.TMDB.value,
                "status": Status.COMPLETED.value,
                "end_date": "2023-06-02",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.season.refresh_from_db()
        self.assertEqual(self.season.status, Status.COMPLETED.value)
        content = response.content.decode()
        self.assertIn(f"track-action-season-{self.season.item.media_id}", content)
        self.assertIn("Completed", content)

    def test_marking_a_season_watched_refreshes_its_own_card_status_chip(
        self,
        _mock_metadata,
    ):
        """Completing a season directly updates its own card's status chip.

        media_card.html renders each season as a card (e.g. on the show
        page) with a status chip that has no OOB target of its own, so a
        season edit never refreshed it — the card kept showing the old
        status until the page was reloaded.
        """
        response = self.client.post(
            reverse("media_save"),
            data={
                "instance_id": self.season.id,
                "media_id": "123",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "status": Status.COMPLETED.value,
                "end_date": "2023-06-02",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.season.refresh_from_db()
        self.assertEqual(self.season.status, Status.COMPLETED.value)
        content = response.content.decode()
        self.assertIn(f'id="media-status-chip-{self.season.id}"', content)
        self.assertIn('hx-swap-oob="outerHTML"', content)
        self.assertIn("M21.801 10A10 10 0 1 1 17 3.335", content)

    def test_completing_the_last_season_refreshes_the_shows_own_pill(
        self,
        _mock_metadata,
    ):
        """Completing a show's only season also refreshes the show's pill.

        Season.save() already cascades the completion to the show
        server-side, but nothing refreshed the show's own status pill
        client-side — marking the last season watched from the show page
        left its "In progress" pill stale until reload.
        """
        response = self.client.post(
            reverse("media_save"),
            data={
                "instance_id": self.season.id,
                "media_id": "123",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "status": Status.COMPLETED.value,
                "end_date": "2023-06-02",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        tv = TV.objects.get(pk=self.tv.pk)
        self.assertEqual(tv.status, Status.COMPLETED.value)
        content = response.content.decode()
        self.assertIn(f"track-action-tv-{self.tv.item.media_id}", content)
        self.assertIn("Completed", content)

    def test_poll_refreshes_the_status_pill_after_an_external_write(
        self,
        _mock_metadata,
    ):
        """A webhook/scrobbler completing the season is also picked up by the poll.

        `episode_history_poll` already re-renders episode rows written with
        no open response to swap into; the season's own status pill needs
        the same treatment.
        """
        with patch(METADATA_PATH, return_value=SEASON_METADATA):
            Episode.objects.create(
                item=self.episode_items[1],
                related_season=self.season,
                end_date=datetime(2023, 6, 2, tzinfo=UTC),
            )
        self.season.refresh_from_db()
        self.assertEqual(self.season.status, Status.COMPLETED.value)

        response = self.client.get(
            reverse("episode_history_poll", args=[self.season.id]),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f"track-action-season-{self.season.item.media_id}", content)
        self.assertIn("Completed", content)

    def test_media_card_renders_its_status_icon(
        self,
        _mock_metadata,
    ):
        """A season card (e.g. on the show page) shows its status icon, not an empty chip.

        media_card.html includes media_card_status_chip.html with `only`,
        which drops everything not explicitly passed — including `Status`,
        normally injected by a context processor. Without passing it
        through explicitly, every `status_value == Status.X.value` check
        in the partial silently fails, so the chip renders but stays
        empty: no icon, just the bare colored box.
        """
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        self.season.status = Status.COMPLETED.value
        self.season.save()

        request = RequestFactory().get("/")
        request.user = self.user
        content = render_to_string(
            "app/components/media_card.html",
            {"item": self.season.item, "media": self.season},
            request=request,
        )

        self.assertIn(f'id="media-status-chip-{self.season.id}"', content)
        self.assertIn("M21.801 10A10 10 0 1 1 17 3.335", content)
