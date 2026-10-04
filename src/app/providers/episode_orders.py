"""Complete, identity-preserving episode catalogues for provider season orders."""

import hashlib
import json
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache

from app.providers import credentials, services, tmdb, tvdb

CACHE_TIMEOUT = 60 * 60
MAX_PAGES = 50
MAX_SEASONS = 1000


def _cache_key(provider, series_id, key, user, language):
    material = json.dumps([
        provider, str(series_id), key, language, getattr(user, "pk", None),
        credentials.cache_suffix(provider, "api_key", "pin", user=user),
    ])
    return "episode_orders_v1_" + hashlib.sha256(material.encode()).hexdigest()


def _request(provider, path, user, language, params=None):
    if provider == "tvdb":
        return tvdb._request(path, params=params, user=user)
    return services.api_request(
        "tmdb", "GET", f"{tmdb.base_url}/{path}",
        params={
            "api_key": credentials.get("tmdb", "api_key", user=user),
            "language": language, **(params or {}),
        },
    )


def _context(provider, series_id, language):
    if provider not in {"tmdb", "tvdb"}:
        message = "Episode ordering requires TMDB or TVDB"
        raise ValueError(message)
    if not str(series_id).isdigit():
        message = "Invalid provider series ID"
        raise ValueError(message)
    return language or settings.TMDB_LANG or "en"


def list_orders(provider, series_id, *, user=None, language=None):
    """Discover orders actually present on a series, including provider default."""
    language = _context(provider, series_id, language)
    cache_key = _cache_key(provider, series_id, "list", user, language)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    if provider == "tmdb":
        response = _request(provider, f"tv/{series_id}/episode_groups", user, language)
        orders = [{"key": "aired", "label": "TMDB (Aired)"}]
        orders.extend(
            {"key": f"group:{row['id']}", "label": row["name"]}
            for row in response["results"]
        )
    else:
        response = _request(provider, f"series/{series_id}/extended", user, language)
        data = response["data"]
        types = data.get("seasonTypes") or [
            row["type"] for row in data["seasons"]
        ]
        orders = [{"key": "default", "label": "TVDB (Series default)"}]
        seen = {"default"}
        for row in types:
            key = row["type"]
            if key not in seen:
                orders.append({"key": key, "label": f"TVDB ({row['name']})"})
                seen.add(key)
    for order in orders:
        order.update(provider=provider, series_id=str(series_id))
    cache.set(cache_key, orders, CACHE_TIMEOUT)
    return orders


def _episode(row, provider, season=None, number=None):
    season = row.get("season_number", row.get("seasonNumber")) if season is None else season
    number = row.get("episode_number", row.get("number")) if number is None else number
    if season is None or number is None or row.get("id") is None:
        message = "Provider episode lacks stable identity or order coordinates"
        raise ValueError(message)
    return {
        "provider_episode_id": str(row["id"]),
        "season_number": int(season), "episode_number": int(number),
        "title": row.get("name") or "",
        "image": tmdb.get_image_url(row.get("still_path"))
        if provider == "tmdb" else row.get("image"),
        "runtime": row.get("runtime"),
        "air_date": row.get("air_date", row.get("aired")),
    }


def _tvdb_episodes(series_id, order_key, user, language):
    episodes = []
    language = tvdb._preferred_language_code(language)
    path = f"series/{series_id}/episodes/{quote(order_key, safe='')}/{language}"
    for page in range(MAX_PAGES):
        response = _request("tvdb", path, user, language, {"page": page})
        rows = response["data"]["episodes"]
        episodes.extend(_episode(row, "tvdb") for row in rows)
        if not (response.get("links") or {}).get("next"):
            return episodes
        if not rows:
            message = "TVDB returned an incomplete episode catalogue"
            raise ValueError(message)
    message = "TVDB episode catalogue exceeds pagination limit"
    raise ValueError(message)


def fetch_order(provider, series_id, order_key, *, user=None, language=None):
    """Fetch a complete catalogue; never cache failed or partial responses."""
    language = _context(provider, series_id, language)
    cache_key = _cache_key(provider, series_id, order_key, user, language)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    orders = list_orders(provider, series_id, user=user, language=language)
    selected = next((row for row in orders if row["key"] == order_key), None)
    if selected is None:
        message = "Episode order is unavailable for this series"
        raise ValueError(message)
    if provider == "tvdb":
        episodes = _tvdb_episodes(series_id, order_key, user, language)
    elif order_key == "aired":
        series = _request(provider, f"tv/{series_id}", user, language)
        seasons = series["seasons"]
        if len(seasons) > MAX_SEASONS:
            message = "TMDB series exceeds season limit"
            raise ValueError(message)
        episodes = []
        for season in seasons:
            number = int(season["season_number"])
            response = _request(provider, f"tv/{series_id}/season/{number}", user, language)
            episodes.extend(_episode(row, provider) for row in response["episodes"])
    else:
        group_id = quote(order_key.removeprefix("group:"), safe="")
        response = _request(provider, f"tv/episode_group/{group_id}", user, language)
        episodes = [
            _episode(row, provider, int(group["order"]), int(row["order"]) + 1)
            for group in response["groups"] for row in group["episodes"]
        ]
    coordinates = {(row["season_number"], row["episode_number"]) for row in episodes}
    identities = {row["provider_episode_id"] for row in episodes}
    if len(coordinates) != len(episodes) or len(identities) != len(episodes):
        message = "Provider catalogue contains duplicate episodes or coordinates"
        raise ValueError(message)
    result = {**selected, "episodes": sorted(
        episodes, key=lambda row: (row["season_number"], row["episode_number"]),
    )}
    cache.set(cache_key, result, CACHE_TIMEOUT)
    return result
