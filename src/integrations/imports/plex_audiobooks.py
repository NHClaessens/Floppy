"""Recognize audiobooks inside a Plex music library.

Plex has no audiobook media type for music sections: an audiobook shelved in a
Plex Music library arrives as ``type="track"`` with the author in
``grandparentTitle``, the book in ``parentTitle`` and the chapter in ``title``.
Floppy therefore has to decide for itself that an album is a book.

These are pure functions over already-fetched Plex payloads so the scoring can
be tuned against fixtures without touching the network.
"""

from __future__ import annotations

import re
import statistics
import unicodedata

# Content kinds a Plex music library can be imported as.
CONTENT_KIND_AUTO = "auto"
CONTENT_KIND_MUSIC = "music"
CONTENT_KIND_AUDIOBOOK = "audiobook"
CONTENT_KINDS = (CONTENT_KIND_AUTO, CONTENT_KIND_MUSIC, CONTENT_KIND_AUDIOBOOK)

# Plex section types that hold music (and therefore possibly audiobooks).
MUSIC_SECTION_TYPES = frozenset({"artist", "music"})

# Substrings in a library title, agent or scanner that mark it as audiobooks.
# Matched against diacritic-folded text, so they are written unaccented here
# ("Hörbücher" folds to "horbucher", which "horbuch" matches).
_SECTION_HINTS = (
    "audiobook",
    "audio book",
    "horbuch",
    "horbuech",
    "hoerbuch",
    "livre audio",
    "audiolibro",
    "audiolivro",
    "ljudbok",
    "luisterboek",
    "audnexus",
    "booksonic",
    "lazyaudiobooks",
    "lazylibrarian",
)

# Genre/style/mood tags that identify spoken-word content. Compared against
# diacritic-folded tag values, so these are written unaccented.
_AUDIOBOOK_TAGS = frozenset(
    {
        "audiobook",
        "audiobooks",
        "audio book",
        "audio books",
        "horbuch",
        "horbucher",
        "hoerbuch",
        "horspiel",
        "hoerspiel",
        "audiolibro",
        "livre audio",
        "spoken",
        "spoken word",
        "spoken & audio",
        "speech",
        "talking book",
        "book",
        "books",
        "literature",
    },
)

# Chapter-style track titles: "Chapter 1", "Part 02", "Kapitel 3", "Track 04".
_CHAPTER_TITLE_RE = re.compile(
    r"^\s*(chapter|chap|ch|part|pt|track|disc|disk|cd|section|kapitel|teil|"
    r"cap[ií]tulo|parte|hoofdstuk|del)\b[\s._-]*\d+",
    re.IGNORECASE,
)

# A chapter runs long; a pop song does not. Milliseconds.
_LONG_TRACK_MS = 8 * 60 * 1000
_SHORT_TRACK_MS = 6 * 60 * 1000
# An audiobook is a long sitting even at its shortest. Milliseconds.
_LONG_ALBUM_MS = 60 * 60 * 1000
_VERY_LONG_ALBUM_MS = 3 * 60 * 60 * 1000
# Book blurbs run long; album liner notes rarely do. Characters.
_BLURB_LENGTH = 300

# Share of tracks that must look like chapters for the title signal to count.
_CHAPTER_TITLE_SHARE = 0.6

# Score at or above which an album is treated as an audiobook.
AUDIOBOOK_SCORE_THRESHOLD = 0.6
# Bonus applied when the album sits in a library that already looks like
# audiobooks; enough to carry a weak-but-positive album over the line, not
# enough to drag an obvious music album across it on its own.
_SECTION_HINT_BONUS = 0.25


def _fold(text: str) -> str:
    """Lowercase and strip diacritics so accented tags match ASCII patterns."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        char for char in decomposed if not unicodedata.combining(char)
    ).lower()


def is_music_section(section: dict) -> bool:
    """Return whether a Plex section holds music."""
    return (section.get("type") or "").lower() in MUSIC_SECTION_TYPES


def section_audiobook_hint(section: dict) -> bool:
    """Return whether a Plex library looks like an audiobook library.

    Matches the library title as well as its metadata agent and scanner, which
    name the audiobook-specific agents (Audnexus, Booksonic) outright.
    """
    haystack = _fold(
        " ".join(str(section.get(key) or "") for key in ("title", "agent", "scanner")),
    )
    return any(hint in haystack for hint in _SECTION_HINTS)


def _tag_values(payload: dict) -> set[str]:
    """Return the lowercased Genre/Style/Mood tag values on a Plex payload."""
    values = set()
    for key in ("Genre", "Style", "Mood"):
        for tag in payload.get(key) or []:
            value = tag.get("tag") if isinstance(tag, dict) else tag
            if value:
                values.add(_fold(str(value).strip()))
    return values


def _track_durations(tracks: list[dict]) -> list[int]:
    """Return positive track durations in milliseconds."""
    durations = []
    for track in tracks:
        try:
            duration = int(track.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            durations.append(duration)
    return durations


def _has_musicbrainz_guid(payload: dict) -> bool:
    """Return whether a Plex payload carries a MusicBrainz GUID.

    Plex's own music agent attaches MBIDs to real music. An audiobook parked in
    a music library never has one, so this is the strongest negative signal.
    """
    guids = payload.get("Guid") or []
    if isinstance(guids, dict):
        guids = [guids]
    for guid in guids:
        value = guid.get("id") if isinstance(guid, dict) else guid
        if value and "mbid" in str(value).lower():
            return True
    guid_value = payload.get("guid")
    return bool(guid_value and "mbid" in str(guid_value).lower())


# A single live scrobble is judged more conservatively than a whole album:
# there is only one track to go on, so the duration bar is higher.
_LONE_TRACK_MS = 15 * 60 * 1000


def track_looks_like_audiobook(metadata: dict) -> bool:
    """Cheap per-track pre-filter for live webhook events.

    A webhook fires per track, so confirming an album costs two Plex API calls
    that must not be spent on every song someone plays. This reads only the
    payload already in hand and errs towards "music": it decides whether an
    event is worth confirming, never whether it is a book.
    """
    if _has_musicbrainz_guid(metadata):
        return False
    if _tag_values(metadata) & _AUDIOBOOK_TAGS:
        return True
    if _CHAPTER_TITLE_RE.match(str(metadata.get("title") or "")):
        return True
    try:
        duration = int(metadata.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return duration >= _LONE_TRACK_MS


def album_audiobook_score(album: dict, tracks: list[dict] | None = None) -> float:
    """Score how much a Plex album looks like an audiobook, from 0.0 to 1.0.

    Args:
        album: Plex album metadata (``/library/metadata/<ratingKey>``)
        tracks: The album's tracks (``/library/metadata/<ratingKey>/children``)
    """
    tracks = tracks or []
    score = 0.0

    if _tag_values(album) & _AUDIOBOOK_TAGS:
        score += 0.45
    elif any(_tag_values(track) & _AUDIOBOOK_TAGS for track in tracks):
        score += 0.35

    durations = _track_durations(tracks)
    total_ms = sum(durations)
    median_ms = statistics.median(durations) if durations else 0

    if median_ms >= _LONG_TRACK_MS and total_ms >= _LONG_ALBUM_MS:
        score += 0.3
    elif durations and median_ms <= _SHORT_TRACK_MS:
        score -= 0.2

    if total_ms >= _VERY_LONG_ALBUM_MS:
        score += 0.1

    titled = [str(track.get("title") or "") for track in tracks]
    if titled:
        chapterish = sum(bool(_CHAPTER_TITLE_RE.match(title)) for title in titled)
        if chapterish / len(titled) >= _CHAPTER_TITLE_SHARE:
            score += 0.2

    if len(str(album.get("summary") or "")) >= _BLURB_LENGTH:
        score += 0.15

    if _has_musicbrainz_guid(album) or any(
        _has_musicbrainz_guid(track) for track in tracks
    ):
        score -= 0.3

    return max(0.0, min(1.0, score))


def is_audiobook_album(
    album: dict,
    tracks: list[dict] | None = None,
    *,
    section_hint: bool = False,
) -> bool:
    """Return whether a Plex album should be tracked as an audiobook.

    Args:
        album: Plex album metadata
        tracks: The album's tracks
        section_hint: Whether the containing library looks like audiobooks
    """
    score = album_audiobook_score(album, tracks)
    if section_hint:
        score = min(1.0, score + _SECTION_HINT_BONUS)
    return score >= AUDIOBOOK_SCORE_THRESHOLD
