"""Kavita importer for manga, comic and book reading progress.

Verified against Kavita's OpenAPI file (v0.9.1.6): an API key is exchanged for
a JWT at ``/api/Plugin/authenticate``, series come from ``/api/Series/all-v2``
with their MyAnimeList and Comic Vine ids, and chapters (with pages read) from
``/api/Series/series-detail``. Nothing is written back to Kavita.
"""

import logging

from django.conf import settings
from django.utils import timezone

import app
from app.models import Item, MediaTypes, Sources
from integrations.imports.helpers import (
    ConnectionAuthError,
    find_item_across_buckets,
)
from integrations.imports.mylar import comic_issue_item
from integrations.imports.reading_server import (
    ReadingServerImporter,
    parse_datetime,
    request_json,
    write_reading_progress,
)
from integrations.models import KavitaAccount, KavitaLink

logger = logging.getLogger(__name__)

ENTRY_SOURCE = "kavita"
PLUGIN_NAME = "Floppy"
PAGE_SIZE = 100

# Kavita's LibraryType enum.
MANGA_LIBRARY = 0
COMIC_LIBRARIES = frozenset({1, 5})
BOOK_LIBRARIES = frozenset({2, 4})  # Book and LightNovel are both epubs

# SeriesFilterField.ReadProgress > 0, sorted by name so paging is stable.
READ_PROGRESS_FIELD = 20
GREATER_THAN = 1
AND_COMBINATION = 1
SORT_BY_NAME = 1


class KavitaClient:
    """Thin API client for the Kavita REST API."""

    def __init__(self, base_url: str, api_key: str):
        """Store the server URL and API key."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._token = None

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self._authenticate()}"}
        return request_json(
            "Kavita",
            method,
            f"{self.base_url}{path}",
            headers=headers,
            **kwargs,
        )

    def _authenticate(self):
        """Exchange the API key for a short-lived token, once per run."""
        if self._token is None:
            payload = request_json(
                "Kavita",
                "post",
                f"{self.base_url}/api/Plugin/authenticate",
                params={"apiKey": self.api_key, "pluginName": PLUGIN_NAME},
            )
            self._token = (payload or {}).get("token")
            if not self._token:
                msg = "Kavita API key is invalid or unauthorized"
                raise ConnectionAuthError(msg)
        return self._token

    def healthcheck(self):
        """Verify the server is reachable and the key is accepted."""
        self._authenticate()

    def series_with_progress(self):
        """Yield every series the user has started reading."""
        body = {
            "statements": [
                {
                    "comparison": GREATER_THAN,
                    "field": READ_PROGRESS_FIELD,
                    "value": "0",
                },
            ],
            "combination": AND_COMBINATION,
            "sortOptions": {"sortField": SORT_BY_NAME, "isAscending": True},
            "limitTo": 0,
        }
        page = 1
        while True:
            series = self._request(
                "post",
                "/api/Series/all-v2",
                params={"PageNumber": page, "PageSize": PAGE_SIZE},
                json=body,
            )
            yield from series or []
            if len(series or []) < PAGE_SIZE:
                return
            page += 1

    def series_detail(self, series_id):
        """Return a series' volumes and chapters with the user's progress."""
        return self._request(
            "get",
            "/api/Series/series-detail",
            params={"seriesId": series_id},
        )


def importer(identifier, user, mode):
    """Import Kavita reading progress for a user."""
    return KavitaImporter(user).import_data()


def mal_manga_item(mal_id, title):
    """Return the MyAnimeList manga Item, creating it from Kavita's data.

    The MAL id is the identity, so no provider lookup is needed; the details
    page fills in the rest the first time it is opened.
    """
    identity = {
        "media_id": str(mal_id),
        "source": Sources.MAL.value,
        "media_type": MediaTypes.MANGA.value,
    }
    existing = find_item_across_buckets(**identity)
    if existing:
        return existing
    item, _ = Item.objects.get_or_create(
        **identity,
        library_media_type=MediaTypes.MANGA.value,
        season_number=None,
        episode_number=None,
        defaults={
            "title": title,
            "original_title": title,
            "localized_title": title,
            "image": settings.IMG_NONE,
        },
    )
    return item


def _chapters(detail):
    """Return each chapter of a series once (volumes, loose chapters, specials)."""
    chapters = {}
    for volume in detail.get("volumes") or []:
        for chapter in volume.get("chapters") or []:
            chapters[chapter.get("id")] = chapter
    for chapter in [*(detail.get("chapters") or []), *(detail.get("specials") or [])]:
        chapters.setdefault(chapter.get("id"), chapter)
    return list(chapters.values())


def _is_read(chapter):
    pages = int(chapter.get("pages") or 0)
    return pages > 0 and int(chapter.get("pagesRead") or 0) >= pages


class KavitaImporter(ReadingServerImporter):
    """Import reading progress from Kavita."""

    service = "Kavita"
    account_attr = "kavita_account"
    account_model = KavitaAccount
    link_model = KavitaLink
    link_field = "kavita_key"
    client_class = KavitaClient

    def sync(self, cutoff, counts, links):
        """Import every started series read after the cutoff."""
        for series in self.client.series_with_progress():
            read_at = parse_datetime(series.get("latestReadDate"))
            if cutoff and read_at and read_at < cutoff:
                continue
            self._import_series(series, read_at or timezone.now(), links, counts)

    def _import_series(self, series, read_at, links, counts):
        """Write one series' progress as a manga, book or comic issues."""
        detail = self.client.series_detail(series["id"])
        library_type = detail.get("libraryType")
        chapters = _chapters(detail)
        if library_type == MANGA_LIBRARY:
            self._import_manga(series, chapters, read_at, links, counts)
        elif library_type in BOOK_LIBRARIES:
            self._import_book(series, chapters, read_at, links, counts)
        elif library_type in COMIC_LIBRARIES:
            for chapter in chapters:
                if int(chapter.get("pagesRead") or 0) > 0:
                    self._import_comic_issue(series, chapter, read_at, links, counts)

    def _import_manga(self, series, chapters, read_at, links, counts):
        """Track a manga series by its chapters read."""
        title = series.get("name") or ""
        read = [chapter for chapter in chapters if _is_read(chapter)]
        # Chapter numbers when Kavita has them, else volumes read.
        progress = max((int(c.get("maxNumber") or 0) for c in read), default=0) or len(
            read,
        )
        pages = int(series.get("pages") or 0)
        completed = pages > 0 and int(series.get("pagesRead") or 0) >= pages
        mal_id = int(series.get("malId") or 0)

        def resolve():
            if mal_id:
                return mal_manga_item(mal_id, title)
            if not self.account.create_missing or not title:
                return None
            return self.manual_item(MediaTypes.MANGA, title)

        self.import_entry(
            links,
            f"series:{series['id']}",
            title,
            counts,
            MediaTypes.MANGA,
            resolve,
            lambda item: write_reading_progress(
                self.user,
                item,
                app.models.Manga,
                progress=progress,
                completed=completed,
                read_at=read_at,
                started_at=None,
                entry_source=ENTRY_SOURCE,
            ),
        )

    def _import_book(self, series, chapters, read_at, links, counts):
        """Track a book (epub) series by pages read."""
        title = series.get("name") or ""
        pages = int(series.get("pages") or 0)
        page = int(series.get("pagesRead") or 0)
        completed = pages > 0 and page >= pages
        authors = list(
            dict.fromkeys(
                writer["name"]
                for chapter in chapters
                for writer in chapter.get("writers") or []
                if writer.get("name")
            ),
        )
        isbn = next((c["isbn"] for c in chapters if c.get("isbn")), "")

        self.import_entry(
            links,
            f"series:{series['id']}",
            title,
            counts,
            MediaTypes.BOOK,
            lambda: self.resolve_book_item(title, authors, isbn),
            lambda item: write_reading_progress(
                self.user,
                item,
                app.models.Book,
                progress=max(pages, page) if completed else page,
                completed=completed,
                read_at=read_at,
                started_at=None,
                entry_source=ENTRY_SOURCE,
            ),
        )

    def _import_comic_issue(self, series, chapter, read_at, links, counts):
        """Track one comic chapter as a comic issue."""
        number = chapter.get("range") or chapter.get("number")
        series_name = series.get("name") or ""
        label = (
            f"{series_name} #{number}"
            if series_name and number
            else chapter.get("titleName") or series_name
        )
        pages = int(chapter.get("pages") or 0)
        page = int(chapter.get("pagesRead") or 0)
        completed = _is_read(chapter)

        def resolve():
            item = comic_issue_item(chapter.get("comicVineId"), label, None)
            if item or not self.account.create_missing or not label:
                return item
            return self.manual_item(MediaTypes.COMIC_ISSUE, label)

        self.import_entry(
            links,
            f"chapter:{chapter['id']}",
            label,
            counts,
            MediaTypes.COMIC_ISSUE,
            resolve,
            lambda item: write_reading_progress(
                self.user,
                item,
                app.models.ComicIssue,
                progress=max(pages, page) if completed else page,
                completed=completed,
                read_at=parse_datetime(chapter.get("lastReadingProgressUtc"))
                or read_at,
                started_at=None,
                entry_source=ENTRY_SOURCE,
            ),
        )
