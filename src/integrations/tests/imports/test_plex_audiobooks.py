"""Tests for recognizing audiobooks inside a Plex music library."""

from django.test import SimpleTestCase

from integrations.imports.plex_audiobooks import (
    AUDIOBOOK_SCORE_THRESHOLD,
    album_audiobook_score,
    is_audiobook_album,
    is_music_section,
    section_audiobook_hint,
)

MINUTE_MS = 60 * 1000


def chapter_tracks(count=12, minutes=32, prefix="Chapter"):
    """Return chapter-shaped tracks of the given length."""
    return [
        {"title": f"{prefix} {index}", "duration": minutes * MINUTE_MS, "index": index}
        for index in range(1, count + 1)
    ]


def song_tracks(count=11, minutes=4):
    """Return song-shaped tracks carrying MusicBrainz GUIDs."""
    return [
        {
            "title": f"Song {index}",
            "duration": minutes * MINUTE_MS,
            "index": index,
            "Guid": [{"id": f"mbid://recording/{index}"}],
        }
        for index in range(1, count + 1)
    ]


class TestSectionHints(SimpleTestCase):
    """The library itself often says it holds audiobooks."""

    def test_title_names_audiobooks(self):
        self.assertTrue(section_audiobook_hint({"title": "Audiobooks"}))
        self.assertTrue(section_audiobook_hint({"title": "Hörbücher"}))

    def test_agent_names_an_audiobook_agent(self):
        self.assertTrue(
            section_audiobook_hint(
                {"title": "Books", "agent": "com.plexapp.agents.audnexus"},
            ),
        )

    def test_ordinary_music_library_is_not_a_hint(self):
        self.assertFalse(
            section_audiobook_hint(
                {"title": "Music", "agent": "tv.plex.agents.music"},
            ),
        )

    def test_music_section_types(self):
        self.assertTrue(is_music_section({"type": "artist"}))
        self.assertTrue(is_music_section({"type": "music"}))
        self.assertFalse(is_music_section({"type": "movie"}))


class TestAlbumScoring(SimpleTestCase):
    """Score albums the way Plex actually presents them."""

    def test_genre_tagged_audiobook(self):
        album = {"title": "Der Prozess", "Genre": [{"tag": "Hörbuch"}]}
        self.assertTrue(is_audiobook_album(album, chapter_tracks()))

    def test_untagged_audiobook_detected_by_shape(self):
        """No usable genre tag: long chapter-titled tracks must carry it."""
        album = {"title": "The Silmarillion"}
        self.assertTrue(is_audiobook_album(album, chapter_tracks()))

    def test_real_music_album_scores_low(self):
        album = {
            "title": "Kind of Blue",
            "Genre": [{"tag": "Jazz"}],
            "Guid": [{"id": "mbid://album/abc"}],
        }
        self.assertLess(
            album_audiobook_score(album, song_tracks()),
            AUDIOBOOK_SCORE_THRESHOLD,
        )
        self.assertFalse(is_audiobook_album(album, song_tracks()))

    def test_musicbrainz_guid_holds_back_a_long_album(self):
        """A matched long-form music album must not be mistaken for a book."""
        album = {"title": "Live at Leeds", "Guid": [{"id": "mbid://album/xyz"}]}
        tracks = [
            {"title": f"Jam {index}", "duration": 20 * MINUTE_MS, "index": index}
            for index in range(1, 8)
        ]
        tracks[0]["Guid"] = [{"id": "mbid://recording/1"}]
        self.assertFalse(is_audiobook_album(album, tracks))

    def test_spoken_word_music_album_is_an_accepted_edge_case(self):
        """A short spoken-word record stays music: chapters are what differ."""
        album = {"title": "Poetry Sessions", "Genre": [{"tag": "Spoken Word"}]}
        tracks = [
            {"title": f"Poem {index}", "duration": 3 * MINUTE_MS, "index": index}
            for index in range(1, 10)
        ]
        self.assertFalse(is_audiobook_album(album, tracks))

    def test_section_hint_carries_a_borderline_album(self):
        """A flagged library nudges an otherwise ambiguous album over."""
        album = {"title": "Untitled Book"}
        tracks = chapter_tracks(count=3, minutes=25, prefix="Part")
        self.assertFalse(is_audiobook_album(album, tracks))
        self.assertTrue(is_audiobook_album(album, tracks, section_hint=True))

    def test_section_hint_does_not_rescue_an_obvious_music_album(self):
        album = {
            "title": "Kind of Blue",
            "Genre": [{"tag": "Jazz"}],
            "Guid": [{"id": "mbid://album/abc"}],
        }
        self.assertFalse(
            is_audiobook_album(album, song_tracks(), section_hint=True),
        )

    def test_score_is_bounded(self):
        album = {
            "title": "Everything",
            "Genre": [{"tag": "Audiobook"}],
            "summary": "x" * 800,
        }
        score = album_audiobook_score(album, chapter_tracks(count=40, minutes=45))
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_empty_album_scores_zero(self):
        self.assertEqual(album_audiobook_score({}, []), 0.0)
