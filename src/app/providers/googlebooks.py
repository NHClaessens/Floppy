"""Google Books metadata provider."""

import re

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import credentials, services

BASE_URL = "https://www.googleapis.com/books/v1/volumes"
IMAGE_LINK_KEYS = (
    "extraLarge",
    "large",
    "medium",
    "small",
    "thumbnail",
    "smallThumbnail",
)
YEAR_RE = re.compile(r"\b(\d{4})\b")


def enabled():
    """Return whether the instance has a Google Books API key."""
    return credentials.is_configured("googlebooks")


def handle_error(error):
    """Handle Google Books API errors."""
    raise services.ProviderAPIError(Sources.GOOGLEBOOKS.value, error)


def search(query, page, language=None, user=None):
    """Search Google Books volumes."""
    language_key = language or "all"
    cache_key = (
        f"search_{Sources.GOOGLEBOOKS.value}_{MediaTypes.BOOK.value}_"
        f"{query}_{language_key}_{page}"
    )
    data = cache.get(cache_key)

    if data is None:
        params = {
            "q": query,
            "startIndex": max(0, (page - 1) * settings.PER_PAGE),
            "maxResults": min(settings.PER_PAGE, 40),
            "printType": "books",
            "key": credentials.get("googlebooks", "api_key", user=user),
        }
        if language:
            params["langRestrict"] = language

        try:
            response = services.api_request(
                Sources.GOOGLEBOOKS.value,
                "GET",
                BASE_URL,
                params=params,
            )
        except requests.RequestException as error:
            handle_error(error)

        results = [
            result
            for item in response.get("items") or []
            if (result := _normalize_search_result(item)) is not None
        ]
        data = helpers.format_search_response(
            page,
            settings.PER_PAGE,
            response.get("totalItems") or 0,
            results,
        )
        cache.set(cache_key, data)

    return data


def book(media_id, user=None):
    """Return normalized metadata for a Google Books volume."""
    cache_key = f"{Sources.GOOGLEBOOKS.value}_{MediaTypes.BOOK.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        try:
            response = services.api_request(
                Sources.GOOGLEBOOKS.value,
                "GET",
                f"{BASE_URL}/{media_id}",
                params={"key": credentials.get("googlebooks", "api_key", user=user)},
            )
        except requests.RequestException as error:
            handle_error(error)

        data = _normalize_book(response, media_id)
        cache.set(cache_key, data)

    return data


def _normalize_search_result(item):
    """Convert one Google Books volume into a search result."""
    volume_info = item.get("volumeInfo") or {}
    media_id = item.get("id")
    title = volume_info.get("title")
    if not media_id or not title:
        return None

    return {
        "media_id": media_id,
        "source": Sources.GOOGLEBOOKS.value,
        "media_type": MediaTypes.BOOK.value,
        "title": title,
        "image": _image_url(volume_info.get("imageLinks")),
        "year": _publication_year(volume_info.get("publishedDate")),
    }


def _normalize_book(response, media_id):
    """Convert a Google Books volume into Floppy's metadata shape."""
    volume_info = response.get("volumeInfo") or {}
    title = volume_info.get("title") or ""
    authors = [
        author
        for author in volume_info.get("authors") or []
        if isinstance(author, str) and author
    ]
    average_rating = volume_info.get("averageRating")
    try:
        score = float(average_rating) * 2 if average_rating is not None else None
    except (TypeError, ValueError):
        score = None

    published_date = volume_info.get("publishedDate")
    source_url = (
        volume_info.get("canonicalVolumeLink")
        or volume_info.get("infoLink")
        or f"https://books.google.com/books?id={media_id}"
    )
    language = volume_info.get("language")
    print_type = volume_info.get("printType")
    isbn = []
    for identifier in volume_info.get("industryIdentifiers") or []:
        if not isinstance(identifier, dict):
            continue
        if identifier.get("type") in {"ISBN_10", "ISBN_13"}:
            value = identifier.get("identifier")
            if value:
                isbn.append(value)

    return {
        "media_id": media_id,
        "source": Sources.GOOGLEBOOKS.value,
        "source_url": source_url,
        "media_type": MediaTypes.BOOK.value,
        "title": title,
        "max_progress": volume_info.get("pageCount"),
        "image": _image_url(volume_info.get("imageLinks")),
        "synopsis": _description(volume_info.get("description")),
        "genres": volume_info.get("categories") or [],
        "score": score,
        "score_count": volume_info.get("ratingsCount") or 0,
        "details": {
            "format": print_type.title() if print_type else None,
            "number_of_pages": volume_info.get("pageCount"),
            "publish_date": published_date,
            "author": authors or None,
            "publisher": volume_info.get("publisher"),
            "isbn": isbn,
            "languages": [language] if language else [],
        },
        "authors_full": [],
        "related": {},
    }


def _image_url(image_links):
    """Choose the highest-resolution available cover image."""
    if not isinstance(image_links, dict):
        return settings.IMG_NONE

    for key in IMAGE_LINK_KEYS:
        image = image_links.get(key)
        if image:
            return str(image).replace("http://", "https://", 1)
    return settings.IMG_NONE


def _publication_year(date_value):
    """Extract the first four-digit publication year."""
    match = YEAR_RE.search(str(date_value or ""))
    return int(match.group(1)) if match else None


def _description(description):
    """Strip markup from an optional Google Books description."""
    # Imported here, not at module scope: beautifulsoup4 is only needed to
    # flatten an HTML description, so importing it at module scope would
    # keep it resident in every process that touches this provider.
    from bs4 import BeautifulSoup

    if not description:
        return "No synopsis available."
    text = BeautifulSoup(str(description), "html.parser").get_text(separator=" ")
    return " ".join(text.split()) or "No synopsis available."
