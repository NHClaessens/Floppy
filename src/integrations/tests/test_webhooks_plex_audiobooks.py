"""Tests for live Plex scrobbles from a music library holding audiobooks."""

import logging
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Book, Item, MediaTypes, Music, Sources, Status
from integrations.models import PlexAccount
from integrations.webhooks.plex import PlexWebhookProcessor

MINUTE_MS = 60 * 1000
MACHINE_ID = "machine-1"
SECTION_ID = "7"


def setUpModule():
    """Silence webhook log noise for this module only."""
    logging.getLogger("integrations.webhooks.plex").setLevel(logging.CRITICAL)


def tearDownModule():
    """Restore the webhook logger level for other modules."""
    logging.getLogger("integrations.webhooks.plex").setLevel(logging.NOTSET)


def scrobble_payload(title="Chapter 3", duration=30 * MINUTE_MS, guid=None):
    """Return a Plex media.scrobble payload for one music track."""
    metadata = {
        "type": "track",
        "ratingKey": "103",
        "parentRatingKey": "100",
        "librarySectionID": SECTION_ID,
        "grandparentTitle": "Franz Kafka",
        "parentTitle": "Der Prozess",
        "title": title,
        "index": 3,
        "duration": duration,
    }
    if guid:
        metadata["Guid"] = [{"id": guid}]
    return {
        "event": "media.scrobble",
        "Account": {"title": "listener"},
        "Server": {"uuid": MACHINE_ID},
        "Metadata": metadata,
    }


def album():
    """Return Plex album metadata for an audiobook."""
    return {
        "ratingKey": "100",
        "type": "album",
        "title": "Der Prozess",
        "parentTitle": "Franz Kafka",
        "thumb": "/library/metadata/100/thumb/1",
        "Genre": [{"tag": "Hörbuch"}],
    }


def chapters(played=0, count=4, minutes=30):
    """Return album tracks, the first `played` of them fully listened to."""
    tracks = []
    for index in range(1, count + 1):
        track = {
            "ratingKey": str(100 + index),
            "title": f"Chapter {index}",
            "index": index,
            "duration": minutes * MINUTE_MS,
        }
        if index <= played:
            track["viewCount"] = 1
            track["lastViewedAt"] = 1735689600
        tracks.append(track)
    return tracks


class PlexAudiobookWebhookTests(TestCase):
    """The live webhook honors the library's configured content type."""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="listener")
        self.user.plex_usernames = "listener"
        self.user.book_enabled = True
        self.user.music_enabled = True
        self.user.save()
        self.account = PlexAccount.objects.create(
            user=self.user,
            plex_token="token",
            plex_username="listener",
            machine_identifier=MACHINE_ID,
            sections=[
                {
                    "id": SECTION_ID,
                    "title": "Audiobooks",
                    "type": "artist",
                    "machine_identifier": MACHINE_ID,
                    "uri": "http://plex.local:32400",
                    "access_token": "token",
                },
            ],
        )

    def process(self, payload, *, album_metadata=None, album_tracks=None):
        """Run a payload through the processor with Plex API calls mocked."""
        with (
            patch(
                "integrations.webhooks.plex.plex_api.fetch_metadata",
                return_value=album_metadata if album_metadata is not None else album(),
            ),
            patch(
                "integrations.webhooks.plex.plex_api.fetch_children",
                return_value=album_tracks if album_tracks is not None else chapters(3),
            ),
        ):
            return PlexWebhookProcessor().process_payload(payload, self.user)

    def set_kind(self, kind):
        """Configure the library's content type."""
        self.account.set_content_kind(MACHINE_ID, SECTION_ID, kind)
        self.account.save()

    def test_flagged_library_updates_the_book(self):
        self.set_kind("audiobook")

        self.process(scrobble_payload())

        item = Item.objects.get(
            source=Sources.PLEX.value,
            media_type=MediaTypes.BOOK.value,
        )
        book = Book.objects.get(user=self.user, item=item)
        self.assertEqual(book.progress, 90)
        self.assertEqual(book.status, Status.IN_PROGRESS.value)
        self.assertFalse(Music.objects.filter(user=self.user).exists())

    def test_later_chapter_advances_the_same_book(self):
        self.set_kind("audiobook")

        self.process(scrobble_payload())
        self.process(scrobble_payload(title="Chapter 4"), album_tracks=chapters(4))

        self.assertEqual(Book.objects.filter(user=self.user).count(), 1)
        book = Book.objects.get(user=self.user)
        self.assertEqual(book.status, Status.COMPLETED.value)
        self.assertEqual(book.progress, 120)

    def test_auto_detects_an_audiobook_scrobble(self):
        self.process(scrobble_payload())

        self.assertTrue(
            Book.objects.filter(item__source=Sources.PLEX.value).exists(),
        )

    def test_book_tracking_disabled_creates_nothing(self):
        self.set_kind("audiobook")
        self.user.book_enabled = False
        self.user.save()

        self.process(scrobble_payload())

        self.assertFalse(Book.objects.filter(user=self.user).exists())

    def test_normal_song_is_untouched_by_audiobook_routing(self):
        """A short, MusicBrainz-matched track never reaches the album lookup."""
        payload = scrobble_payload(
            title="So What",
            duration=9 * MINUTE_MS,
            guid="mbid://recording/abc",
        )

        with patch(
            "integrations.webhooks.plex.PlexWebhookProcessor._fetch_audiobook_album",
        ) as mock_fetch:
            processor = PlexWebhookProcessor()
            self.assertIsNone(processor._audiobook_routing(payload, self.user))
            mock_fetch.assert_not_called()

    def test_album_is_fetched_from_the_payloads_own_server(self):
        """Rating keys are server-local, so a multi-server account must match.

        A Plex webhook's Server block carries a uuid but no uri, so resolving
        by "first cached section" would fetch the album from the wrong server.
        """
        self.account.sections = [
            {
                "id": "2",
                "title": "Audiobooks",
                "type": "artist",
                "machine_identifier": "other-machine",
                "uri": "http://wrong.local:32400",
                "access_token": "wrong-token",
            },
            *self.account.sections,
        ]
        self.account.save()
        self.set_kind("audiobook")

        with (
            patch(
                "integrations.webhooks.plex.plex_api.fetch_metadata",
                return_value=album(),
            ) as mock_metadata,
            patch(
                "integrations.webhooks.plex.plex_api.fetch_children",
                return_value=chapters(3),
            ),
        ):
            PlexWebhookProcessor().process_payload(scrobble_payload(), self.user)

        token, uri, _ = mock_metadata.call_args[0]
        self.assertEqual(uri, "http://plex.local:32400")
        self.assertEqual(token, "token")

    def test_shared_server_uses_the_sections_access_token(self):
        """A server shared by another user needs that section's own token."""
        self.account.sections = [
            {
                **self.account.sections[0],
                "access_token": "friend-token",
            },
        ]
        self.account.save()
        self.set_kind("audiobook")

        with (
            patch(
                "integrations.webhooks.plex.plex_api.fetch_metadata",
                return_value=album(),
            ) as mock_metadata,
            patch(
                "integrations.webhooks.plex.plex_api.fetch_children",
                return_value=chapters(3),
            ),
        ):
            PlexWebhookProcessor().process_payload(scrobble_payload(), self.user)

        self.assertEqual(mock_metadata.call_args[0][0], "friend-token")

    def test_section_hint_is_applied_to_live_detection(self):
        """Auto detection must agree with the importer on a hinted library.

        Otherwise a borderline album imports as a book from history while its
        live scrobbles are recorded as music.
        """
        borderline = {"ratingKey": "100", "type": "album", "title": "Untitled Book"}
        tracks = [
            {
                "ratingKey": str(100 + index),
                "title": f"Part {index}",
                "index": index,
                "duration": 25 * MINUTE_MS,
            }
            for index in range(1, 4)
        ]

        processor = PlexWebhookProcessor()
        payload = scrobble_payload(title="Part 3")
        with (
            patch(
                "integrations.webhooks.plex.plex_api.fetch_metadata",
                return_value=borderline,
            ),
            patch(
                "integrations.webhooks.plex.plex_api.fetch_children",
                return_value=tracks,
            ),
        ):
            # The cached section is titled "Audiobooks", so the hint applies.
            self.assertTrue(processor._confirm_audiobook_album(payload, self.user))

    def test_music_choice_skips_the_musicbrainz_search(self):
        """Chapters kept as music must not be fuzzy-matched to MusicBrainz.

        Searching MusicBrainz for "Chapter 3" only ever returns a junk match,
        so the event is recorded from the local Plex tags instead.
        """
        self.set_kind("music")
        payload = scrobble_payload()

        processor = PlexWebhookProcessor()
        self.assertEqual(
            processor._audiobook_routing(payload, self.user),
            "music_no_lookup",
        )

        with patch(
            "app.services.music_scrobble._populate_from_search",
        ) as mock_search:
            self.process(payload)
            mock_search.assert_not_called()

        self.assertFalse(
            Item.objects.filter(
                source=Sources.PLEX.value,
                media_type=MediaTypes.BOOK.value,
            ).exists(),
        )
