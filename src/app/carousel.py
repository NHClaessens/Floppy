"""Media-details carousel (trailer + photos) resolution.

Distinct from ``backdrops.py``: that module returns a single horizontal image
(or ``None``) for card art, eagerly consulted on hot paths. This module
returns a multi-item {"video", "photos"} payload for the details page's
carousel, fetched lazily (only when the page's carousel fragment is
requested) and only for the media types/sources that actually expose a
trailer or photo gallery.
"""

from django.core.cache import cache

from app.image_cache import rewrite_image_url
from app.models import MediaTypes, Sources
from app.providers import tmdb

_TMDB_CAROUSEL_TYPES = (MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.SEASON.value)


def carousel_supported(media_type, source):
    """Return whether the given media_type/source combination can have a carousel."""
    if source == Sources.TMDB.value and media_type in _TMDB_CAROUSEL_TYPES:
        return True
    return bool(source == Sources.IGDB.value and media_type == MediaTypes.GAME.value)


def confirmed_empty(media_type, source, media_id, *, season_number=None) -> bool:
    """Return True if a prior fetch already confirmed there's no trailer/photos.

    Cache peek only (never fetches), so a page whose carousel was already
    found empty on an earlier view can render the plain, non-carousel layout
    up front instead of paying for the lazy carousel round trip again.
    """
    if source == Sources.TMDB.value and media_type in _TMDB_CAROUSEL_TYPES:
        data = tmdb.peek_carousel_media(media_type, media_id, season_number=season_number)
    elif source == Sources.IGDB.value and media_type == MediaTypes.GAME.value:
        data = cache.get(f"igdb_carousel_v2_{media_id}")
    else:
        return False
    return data is not None and not data["video"] and not data["photos"]


def resolve_carousel_media(media_type, source, media_id, *, season_number=None) -> dict | None:
    """Return {"video": {...}|None, "photos": [{"url", "thumb_url"}, ...]} or None."""
    if source == Sources.TMDB.value and media_type in _TMDB_CAROUSEL_TYPES:
        data = tmdb.carousel_media(media_type, media_id, season_number=season_number)
        photos = [
            {
                "url": rewrite_image_url(tmdb.get_carousel_image_url(photo["file_path"], size="w1280")),
                "thumb_url": rewrite_image_url(tmdb.get_carousel_image_url(photo["file_path"], size="w300")),
            }
            for photo in data["photos"]
        ]
        if not data["video"] and not photos:
            return None
        return {"video": data["video"], "photos": photos}

    if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value:
        from lists.models import CustomList

        data = CustomList()._get_igdb_carousel_media(media_id)
        photos = [
            {
                # t_screenshot_big_2x and similar named IGDB transforms crop to a
                # fixed canvas; t_1080p only caps resolution, so it keeps the
                # source image's real aspect ratio for the main pane/lightbox.
                "url": rewrite_image_url(
                    f"https://images.igdb.com/igdb/image/upload/t_1080p/{image_id}.jpg"
                ),
                "thumb_url": rewrite_image_url(
                    f"https://images.igdb.com/igdb/image/upload/t_screenshot_big_2x/{image_id}.jpg"
                ),
            }
            for image_id in data["photos"]
        ]
        if not data["video"] and not photos:
            return None
        return {"video": data["video"], "photos": photos}

    return None
