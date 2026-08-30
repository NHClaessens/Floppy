"""TVMaze external-ID resolution.

Not a full media-source provider (no search/metadata/UI surface, see
`app.providers.tvdb` for that shape) - just enough to cross-reference a
TVMaze show id to TVDB/IMDB so webhook payloads that only carry a TVMaze id
(e.g. Kodi) can still be resolved to a TMDB show via the existing
`app.providers.tmdb.find` lookup. TVMaze's public API is unauthenticated, so
no API key/settings are needed.
"""

import logging

from django.core.cache import cache

from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://api.tvmaze.com"
CACHE_TIMEOUT = 60 * 60 * 12


def external_ids(tvmaze_id):
    """Return {"tvdb_id": ..., "imdb_id": ...} for a TVMaze show id, or None."""
    if not tvmaze_id:
        return None

    cache_key = f"tvmaze_externals_{tvmaze_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    try:
        response = services.api_request(
            "tvmaze",
            "GET",
            f"{base_url}/shows/{tvmaze_id}",
        )
    except services.ProviderAPIError as exc:
        logger.debug("TVMaze show lookup failed for %s: %s", tvmaze_id, exc)
        cache.set(cache_key, {}, timeout=CACHE_TIMEOUT)
        return None

    externals = (response or {}).get("externals") or {}
    result = {
        "tvdb_id": str(externals["thetvdb"]) if externals.get("thetvdb") else None,
        "imdb_id": externals.get("imdb"),
    }
    result = result if (result["tvdb_id"] or result["imdb_id"]) else {}

    cache.set(cache_key, result, timeout=CACHE_TIMEOUT)
    return result or None
