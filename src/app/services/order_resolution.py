"""Explicit boundaries between provider coordinates and a user's episode order."""

# Messages are user-facing resolution details and remain beside their failure
# branch for clear integration diagnostics.
# ruff: noqa: EM101, TRY003

from django.db.models import Q

from app.models import TV, EpisodeOrder, Item, MediaTypes


class OrderResolutionError(ValueError):
    """An episode cannot be identified safely in the selected tracking order."""


def order_from_media_id(media_id, source=None):
    """Resolve an internal order identity without ever sending it to a provider."""
    value = str(media_id)
    if not value.startswith("order_"):
        return None
    suffix = value.removeprefix("order_")
    if not suffix.isdecimal():
        raise OrderResolutionError("Invalid episode order identity.")
    query = EpisodeOrder.objects.select_related("show").filter(pk=int(suffix))
    if source is not None:
        query = query.filter(provider=source)
    order = query.first()
    if order is None:
        raise OrderResolutionError("Episode order is unavailable.")
    return order


def active_order(user, media_id, source):
    """Return a personal order only for a positively identified tracked show."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    order = order_from_media_id(media_id, source)
    if order is not None:
        return order if TV.objects.filter(user=user, active_episode_order=order).exists() else None
    trackers = TV.objects.filter(user=user, active_episode_order__isnull=False).filter(
        Q(item__media_id=str(media_id), item__source=source)
        | Q(active_episode_order__series_id=str(media_id), active_episode_order__provider=source)
    ).select_related("active_episode_order", "active_episode_order__show")
    matches = list(trackers[:2])
    if len(matches) > 1:
        raise OrderResolutionError("Multiple tracked shows match this provider identity.")
    return matches[0].active_episode_order if matches else None


def resolve_incoming_episode(
    user, media_id, source, season_number, episode_number, *,
    provider_episode_id=None, source_order=None,
):
    """Translate a proven provider episode identity; never guess an offset."""
    order = active_order(user, media_id, source)
    if order is None:
        return None
    if str(media_id) == order.media_id:
        items = list(Item.objects.filter(
            episode_order=order, media_type=MediaTypes.EPISODE.value,
            season_number=season_number, episode_number=episode_number,
        ))
        if len(items) == 1:
            return items
        raise OrderResolutionError("Episode is absent from the selected order.")
    if not provider_episode_id:
        from app.providers.episode_orders import fetch_order

        catalogue = fetch_order(
            source, str(media_id),
            source_order or ("aired" if source == "tmdb" else "default"),
            user=user,
        )
        matches = [
            row for row in catalogue["episodes"]
            if row["season_number"] == season_number
            and row["episode_number"] == episode_number
        ]
        if len(matches) != 1:
            raise OrderResolutionError("Source episode needs an explicit mapping.")
        provider_episode_id = matches[0]["provider_episode_id"]
    if source == order.provider:
        items = list(Item.objects.filter(
            episode_order=order, media_type=MediaTypes.EPISODE.value,
            provider_episode_id=str(provider_episode_id),
        ))
        if len(items) == 1:
            return items
    raise OrderResolutionError("Episode needs an approved mapping to the selected order.")
