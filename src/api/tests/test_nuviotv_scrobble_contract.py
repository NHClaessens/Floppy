"""What Floppy does with the playback events NuvioTV sends.

The bodies below are the ones NuvioTV's Floppy tracker builds
(`FloppyScrobbleBodyTest` in the NuvioTV repository). NuvioTV only knows a
percentage, never seconds, so a stop carries an explicit `completed` flag
(true from 80%) and no position. Start and pause carry neither. NuvioTV does
not send a stop under 1%, which is its own cut-off for a skim.

If one of these breaks, a shipped NuvioTV breaks with it.
"""

from http import HTTPStatus as HTTP  # noqa: N814
from unittest.mock import patch

from django.utils import timezone

from app.models import Movie, Status
from integrations.models import IntegrationToken

from .base import FloppyApiTestCase

SCROBBLE = "/api/v1/scrobble/"

MOVIE_START = {
    "action": "start",
    "media_type": "movie",
    "ids": {"imdb": "tt0133093", "tmdb": "603"},
    "title": "The Matrix",
}
MOVIE_STOP_FINISHED = {**MOVIE_START, "action": "stop", "completed": True}
MOVIE_STOP_ABANDONED = {**MOVIE_START, "action": "stop", "completed": False}
EPISODE_STOP_FINISHED = {
    "action": "stop",
    "media_type": "episode",
    "ids": {"imdb": "tt11280740"},
    "series_title": "Severance",
    "title": "Good News About Hell",
    "season_number": 1,
    "episode_number": 1,
    "completed": True,
}

MATRIX = {"title": "The Matrix", "image": "https://example.com/matrix.jpg"}


class NuvioTvScrobbleContractTests(FloppyApiTestCase):
    """The default token NuvioTV is told to create can do everything it needs."""

    def setUp(self):
        """Mint the default-preset token a user pastes into NuvioTV."""
        super().setUp()
        self.token, self.secret = IntegrationToken.generate(
            user=self.user1,
            name="NuvioTV",
        )
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.secret}"}
        tmdb_movie = patch("app.providers.tmdb.movie", return_value=MATRIX)
        self.tmdb_movie = tmdb_movie.start()
        self.addCleanup(tmdb_movie.stop)

    def _scrobble(self, payload, headers=None):
        return self.client.post(
            SCROBBLE,
            payload,
            format="json",
            **(self.headers if headers is None else headers),
        )

    def _matrix(self):
        return Movie.objects.filter(user=self.user1, item__media_id="603")

    def test_start_and_pause_are_accepted_and_record_no_history(self):
        """Playback in progress is a live card, never a watch."""
        for action in ("start", "pause"):
            with self.subTest(action=action):
                response = self._scrobble({**MOVIE_START, "action": action})

                self.assertEqual(response.status_code, HTTP.OK)
        self.assertFalse(self._matrix().exists())

    def test_finishing_a_movie_records_one_completed_watch(self):
        """A stop at or above NuvioTV's 80% mark completes the movie."""
        response = self._scrobble(MOVIE_STOP_FINISHED)

        self.assertEqual(response.status_code, HTTP.OK)
        movie = self._matrix().get()
        self.assertEqual(movie.status, Status.COMPLETED.value)

    def test_stopping_early_tracks_the_film_as_in_progress_not_watched(self):
        """A stop under 80% is a real end signal: In Progress, never Completed.

        Floppy cannot tell a skim from a pause-and-quit without a position, so
        NuvioTV does not send a stop under 1% (see `floppyScrobbleBody`).
        """
        response = self._scrobble(MOVIE_STOP_ABANDONED)

        self.assertEqual(response.status_code, HTTP.OK)
        self.assertEqual(self._matrix().get().status, Status.IN_PROGRESS.value)

    def test_a_movie_known_only_by_imdb_is_matched_through_tmdb(self):
        """Stremio-style ids are IMDb first; Floppy resolves them, never by title."""
        payload = {**MOVIE_STOP_FINISHED, "ids": {"imdb": "tt0133093"}}
        found = {"movie_results": [{"id": 603}], "tv_results": []}

        with patch("app.providers.tmdb.find", return_value=found):
            response = self._scrobble(payload)

        self.assertEqual(response.status_code, HTTP.OK)
        self.assertTrue(self._matrix().exists())

    def test_ids_sent_as_numbers_are_accepted_too(self):
        """Other clients send tmdb as a JSON number; that must not be a 500."""
        payload = {**MOVIE_STOP_FINISHED, "ids": {"tmdb": 603}}

        response = self._scrobble(payload)

        self.assertEqual(response.status_code, HTTP.OK)
        self.assertTrue(self._matrix().exists())

    @patch("api.fork_views_scrobble.GenericScrobbleProcessor.process_payload")
    def test_an_episode_stop_carries_what_floppy_needs_to_find_it(self, process):
        """Show title, coordinates and ids reach the processor unchanged."""
        response = self._scrobble(EPISODE_STOP_FINISHED)

        self.assertEqual(response.status_code, HTTP.OK)
        sent = process.call_args.args[0]
        self.assertEqual(sent["media_type"], "episode")
        self.assertEqual(sent["ids"], {"imdb": "tt11280740"})
        self.assertEqual(sent["series_title"], "Severance")
        self.assertEqual((sent["season_number"], sent["episode_number"]), (1, 1))
        self.assertIs(sent["completed"], True)

    def test_an_unresolvable_stop_is_a_client_error_not_a_crash(self):
        """NuvioTV logs the failure and moves on; it must be a 4xx, not a 500."""
        with patch(
            "app.providers.tmdb.movie",
            side_effect=RuntimeError("tmdb down"),
        ):
            response = self._scrobble(MOVIE_STOP_FINISHED)

        self.assertEqual(response.status_code, HTTP.NOT_FOUND)

    def test_a_revoked_token_is_refused(self):
        """Disconnecting in Floppy takes effect on NuvioTV's next event."""
        self.token.revoked_at = timezone.now()
        self.token.save(update_fields=["revoked_at"])

        response = self._scrobble(MOVIE_START)

        self.assertEqual(response.status_code, HTTP.FORBIDDEN)

    def test_a_token_without_the_scrobble_permission_is_refused(self):
        """The permission is named, so the user can see why it failed."""
        _token, secret = IntegrationToken.generate(
            user=self.user1,
            name="Read only",
            scopes=["progress:read"],
        )

        response = self._scrobble(
            MOVIE_START,
            headers={"HTTP_AUTHORIZATION": f"Bearer {secret}"},
        )

        self.assertEqual(response.status_code, HTTP.FORBIDDEN)

    def test_the_connection_check_nuviotv_uses_works_with_the_default_token(self):
        """NuvioTV's Connect button reads the sync connections feed."""
        response = self.client.get("/api/v1/sync/connections/", **self.headers)

        self.assertEqual(response.status_code, HTTP.OK)

    def test_the_connection_check_refuses_a_bad_token(self):
        """A typo in the pasted token is reported, not accepted."""
        response = self.client.get(
            "/api/v1/sync/connections/",
            HTTP_AUTHORIZATION="Bearer flp_not_a_real_token",
        )

        self.assertEqual(response.status_code, HTTP.FORBIDDEN)
