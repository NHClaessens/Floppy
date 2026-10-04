"""Which tracker rows put an ``Item`` in a user's library for a media type.

The engine queries ``Item`` rows and reaches tracker state through correlated
subqueries, so one item is one candidate however many tracker rows (repeat
viewings) it has. This module is the single place that knows how each media
type's tracker model is reached: the owner path, the status path, and the
anime-library routing that lets TV-tracked anime appear in the Anime library.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps
from django.db import models
from django.db.models import Case, F, OuterRef, Q, When

from app.library_query.spec import ROUTING_MODEL
from app.models.choices import MediaTypes
from app.services import metadata_resolution


@dataclass(frozen=True)
class TrackerSource:
    """One tracker model feeding a library, and the items it may contribute."""

    model: type[models.Model]
    user_lookup: str
    status_field: str
    item_q: Q

    @property
    def is_episode(self) -> bool:
        """Return whether rows hang off a season instead of a user."""
        return self.user_lookup != "user"

    def rows(self, user):
        """Return every tracker row the user owns in this model."""
        return self.model.objects.filter(**{self.user_lookup: user})

    def item_rows(self, user, outer_ref: str = "pk"):
        """Return the user's rows for the outer item (a correlated subquery).

        The owner is compared as ``owner + 0`` on purpose. Without table
        statistics SQLite otherwise picks the (user, created_at) index to
        satisfy a subquery's ORDER BY and walks every row the user owns, once
        per item; an expression cannot use an index, so the lookup goes
        through ``item_id`` and touches only the item's own rows.
        """
        user_id = getattr(user, "pk", user)
        return (
            self.model.objects.filter(item_id=OuterRef(outer_ref))
            .alias(_owner=F(self.user_lookup) + 0)
            .filter(_owner=user_id)
        )

    def item_ids(self, user, row_q: Q | None = None):
        """Return the ids of items with a user's row matching ``row_q``.

        Uncorrelated on purpose: ``pk IN (this)`` is evaluated once from the
        tracker table's user index, where ``EXISTS`` correlated to each item
        would make the database walk every user's items.
        """
        rows = self.rows(user)
        if row_q is not None:
            rows = rows.filter(row_q)
        return rows.values("item_id")

    def has_field(self, name: str) -> bool:
        """Return whether rows store ``name`` (TV derives dates from seasons)."""
        return any(field.attname == name for field in self.model._meta.concrete_fields)

    def activity(self):
        """Return the expression that orders rows by most recent activity."""
        whens = [
            When(**{f"{name}__isnull": False}, then=F(name))
            for name in ("end_date", "progressed_at")
            if self.has_field(name)
        ]
        if not whens:
            return F("created_at")
        return Case(*whens, default=F("created_at"), output_field=models.DateTimeField())


def _source(media_type: str, item_q: Q | None = None) -> TrackerSource:
    model = apps.get_model("app", media_type)
    if media_type == MediaTypes.EPISODE.value:
        return TrackerSource(
            model=model,
            user_lookup="related_season__user",
            status_field="related_season__status",
            item_q=item_q or Q(),
        )
    return TrackerSource(
        model=model,
        user_lookup="user",
        status_field="status",
        item_q=item_q or Q(),
    )


def tracker_sources(user, media_type: str, routing: str) -> list[TrackerSource]:
    """Return the tracker sources that make up ``media_type``'s library.

    Anime a user tracks as TV lives on TV rows with an anime library bucket.
    With ``library`` routing, ``anime_library_visibility`` decides whether
    those rows appear in the Anime library, the TV library, or both, exactly
    as the media list does. With ``model`` routing they stay on TV.
    """
    if routing == ROUTING_MODEL:
        return [_source(media_type)]
    anime_bucket = Q(library_media_type=MediaTypes.ANIME.value)
    if media_type == MediaTypes.ANIME.value:
        include_in_anime, _include_in_tv = metadata_resolution.anime_library_visibility(
            user,
        )
        sources = [_source(MediaTypes.ANIME.value)]
        if include_in_anime:
            sources.append(_source(MediaTypes.TV.value, anime_bucket))
        return sources
    if media_type == MediaTypes.TV.value:
        _include_in_anime, include_in_tv = metadata_resolution.anime_library_visibility(
            user,
        )
        return [_source(MediaTypes.TV.value, None if include_in_tv else ~anime_bucket)]
    return [_source(media_type)]
