"""Exact, fail-closed classification and promotion for grouped anime.

Grouped anime is stored as TV-shaped records with ``library_media_type`` set to
``anime``.  This module is deliberately provider-agnostic at the call sites:
Stremio imports, Stremio webhooks, and the repair command all use the same
classification and in-place promotion code.

The classifier does not use title matching.  A show must have an exact
Anime-IDs external-ID match and TMDB must identify it as Animation.  This is
intentionally conservative: a false negative leaves a show in TV, while a
false positive would move history into the wrong library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from app.models import Episode, Item, MediaTypes, Season, Sources
from app.services import metadata_resolution
from integrations import anime_mapping

MAPPING_SOURCE = "Kometa Anime-IDs"
ANIMATION_GENRE = "animation"
GROUPED_BUCKET = MediaTypes.ANIME.value
GROUPED_PARENT_TYPES = {Sources.TMDB.value, Sources.TVDB.value}
CLASSIFIER_CALL_COUNT = 0

# Distinguishes "no snapshot supplied, load it lazily" from "a load was
# attempted and failed". Collapsing these two into `None` turns the classifier
# fail-open, which is how anime silently leaks into the TV library.
UNSET = object()

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GroupedAnimeMatch:
    """The auditable result of one exact grouped-anime classification."""

    decision: str
    reason: str
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    mal_ids: tuple[str, ...] = ()
    matched_by: tuple[str, ...] = ()
    mapping_keys: tuple[str, ...] = ()
    mapping_source: str = MAPPING_SOURCE
    mapping_revision: str | None = None
    mapping_digest: str | None = None
    mapping_group_key: str | None = None
    candidate_group_keys: tuple[str, ...] = ()

    @property
    def is_grouped_anime(self) -> bool:
        """Return whether this result is safe to route to grouped anime."""
        return self.decision == "move"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON/report-friendly representation."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "imdb_id": self.imdb_id,
            "mal_ids": list(self.mal_ids),
            "matched_by": list(self.matched_by),
            "mapping_keys": list(self.mapping_keys),
            "mapping_source": self.mapping_source,
            "mapping_revision": self.mapping_revision,
            "mapping_digest": self.mapping_digest,
            "mapping_group_key": self.mapping_group_key,
            "candidate_group_keys": list(self.candidate_group_keys),
        }


def _normalise_ids(value: Any) -> set[str]:
    """Normalize scalar, list, and comma-separated external IDs."""
    if value in (None, ""):
        return set()
    values = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    return {str(entry).strip() for entry in values if str(entry).strip()}


def build_mapping_indexes(
    mapping_data: dict[str, Any],
) -> dict[str, dict[str, tuple[dict[str, Any], ...]]]:
    """Keep the legacy helper while delegating compilation to snapshots."""
    indexes, _groups = anime_mapping.build_mapping_indexes(
        mapping_data,
        revision="inline",
    )
    return indexes


def _metadata_external_ids(metadata: dict[str, Any]) -> dict[str, str]:
    """Extract the show-level IDs exposed by TMDB-shaped metadata."""
    external_ids = dict(metadata.get("provider_external_ids") or {})
    external_ids.update(metadata.get("external_ids") or {})
    if metadata.get("media_id") not in (None, ""):
        external_ids.setdefault("tmdb_id", str(metadata["media_id"]))
    if metadata.get("tvdb_id") not in (None, ""):
        external_ids.setdefault("tvdb_id", str(metadata["tvdb_id"]))
    tmdb_id = next(
        (
            external_ids.get(field)
            for field in ("tmdb_show_id", "tmdb_id", "tmdb_tv_id")
            if external_ids.get(field) not in (None, "")
        ),
        None,
    )
    if tmdb_id:
        for field in ("tmdb_show_id", "tmdb_id", "tmdb_tv_id"):
            external_ids.setdefault(field, tmdb_id)

    return {
        key: str(value).strip()
        for key, value in external_ids.items()
        if value not in (None, "")
    }


class AnimeRouteResolver:
    """Decide which library an imported show belongs in, once per run.

    Importers buffer their rows and flush at the end of a run, so a database
    lookup cannot see a home created earlier in the same import. Without the
    run cache below, one run can open both a grouped and a flat home for the
    same show - manufacturing exactly the duplicates the repair task exists to
    clean up.

    `snapshot` follows `classify`: UNSET to load lazily, None when a load was
    already attempted and failed, or a loaded snapshot to reuse.
    """

    def __init__(self, user, *, snapshot=UNSET):
        """Start a routing run for one user."""
        self.user = user
        self.snapshot = snapshot
        self._routes = {}
        self._mal_routes = {}
        self._verdicts = {}

    def _cache_keys(self, tmdb_id, tvdb_id):
        keys = []
        if tmdb_id:
            keys.append((Sources.TMDB.value, str(tmdb_id)))
        if tvdb_id:
            keys.append((Sources.TVDB.value, str(tvdb_id)))
        return keys

    def route_for_show(self, tv_metadata, *, tmdb_id, tvdb_id=None):
        """Return "grouped", "flat", or None to leave the show in TV."""
        if not getattr(self.user, "anime_enabled", False):
            return None

        keys = self._cache_keys(tmdb_id, tvdb_id)
        for key in keys:
            if key in self._routes:
                return self._routes[key]

        route = self._resolve(tv_metadata, tmdb_id, tvdb_id)
        for key in keys:
            self._routes[key] = route
        return route

    def _resolve(self, tv_metadata, tmdb_id, tvdb_id):
        home = metadata_resolution.find_existing_anime_home(
            self.user,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
        )
        if home is not None:
            return home[0]

        if not metadata_resolution.prefers_grouped_anime(self.user):
            # A MAL-preferring user's anime belongs in flat rows. Importers
            # without a MAL identity treat this as "not mine to create".
            return "flat" if self.verdict_is_anime(tv_metadata, tmdb_id=tmdb_id) else None

        return "grouped" if self.verdict_is_anime(tv_metadata, tmdb_id=tmdb_id) else None

    def verdict(self, tv_metadata, *, tmdb_id=None):
        """Return the classifier verdict, computed at most once per show."""
        key = str(tmdb_id) if tmdb_id else id(tv_metadata)
        if key not in self._verdicts:
            self._verdicts[key] = classify(tv_metadata, snapshot=self.snapshot)
        return self._verdicts[key]

    def verdict_is_anime(self, tv_metadata, *, tmdb_id=None):
        """Return whether the classifier positively identifies this as anime."""
        match = self.verdict(tv_metadata, tmdb_id=tmdb_id)
        return match is not None and match.is_grouped_anime

    def remember(self, route, *, tmdb_id=None, tvdb_id=None, mal_ids=()):
        """Record a route decided elsewhere so the rest of the run agrees.

        `mal_ids` comes from a grouped verdict (`GroupedAnimeMatch.mal_ids`) and
        lets a later flat-identity entry in the same run recognise that this
        show already has a home, before it opens a second one.
        """
        for key in self._cache_keys(tmdb_id, tvdb_id):
            self._routes[key] = route
        for mal_id in mal_ids or ():
            self._mal_routes[str(mal_id)] = route

    def route_for_mal_id(self, mal_id):
        """Return the route already chosen this run for a MAL identity."""
        return self._mal_routes.get(str(mal_id))


def classify(tv_metadata, *, snapshot=UNSET):
    """Return the grouped-anime verdict for a show, or None when unknown.

    Returns None both when the show is not anime and when the Anime-IDs
    snapshot could not be loaded: grouping is fail-closed on load failure, so
    the ordinary TV path still records progress.

    `snapshot` is UNSET to load lazily, None when a load was already attempted
    and failed, or a loaded snapshot to use.
    """
    if snapshot is None:
        return None
    if snapshot is UNSET:
        return classify_tv_metadata(tv_metadata)
    return classify_tv_metadata(tv_metadata, snapshot=snapshot)


def classify_tv_metadata(
    metadata: dict[str, Any] | None,
    *,
    mapping_data: dict[str, Any] | None = None,
    snapshot: anime_mapping.AnimeMappingSnapshot | None = None,
) -> GroupedAnimeMatch:
    """Classify one TMDB TV metadata payload using exact external IDs."""
    metadata = metadata or {}
    ids = _metadata_external_ids(metadata)
    snapshot = snapshot or anime_mapping.load_mapping_snapshot(mapping_data)
    global CLASSIFIER_CALL_COUNT  # noqa: PLW0603 - process-local audit counter

    CLASSIFIER_CALL_COUNT += 1
    logger.info(
        "grouped_anime_classifier_call count=%s revision=%s digest=%s",
        CLASSIFIER_CALL_COUNT,
        snapshot.revision,
        snapshot.digest,
    )
    indexes = snapshot.indexes

    lookup_fields = {
        "tmdb": (
            "tmdb_show_id",
            "tmdb_id",
            "tmdb_tv_id",
        ),
        "tvdb": ("tvdb_id",),
        "imdb": ("imdb_id",),
    }
    hits: list[tuple[str, dict[str, Any]]] = []
    for provider, fields in lookup_fields.items():
        for field in fields:
            external_id = ids.get(field)
            if not external_id:
                continue
            hits.extend(
                (provider, entry) for entry in indexes[field].get(external_id, [])
            )

    candidate_group_keys = tuple(
        sorted({entry["mapping_group_key"] for _provider, entry in hits}),
    )
    matched_entries = {
        entry["mapping_key"]: entry for _provider, entry in hits
    }
    mal_ids = tuple(sorted({mal_id for entry in matched_entries.values() for mal_id in entry["mal_ids"]}))
    result_kwargs = {
        "tmdb_id": ids.get("tmdb_id"),
        "tvdb_id": ids.get("tvdb_id"),
        "imdb_id": ids.get("imdb_id"),
        "mal_ids": mal_ids,
        "matched_by": tuple(sorted({provider for provider, _entry in hits})),
        "mapping_keys": tuple(sorted(matched_entries)),
        "mapping_revision": snapshot.revision,
        "mapping_digest": snapshot.digest,
        "candidate_group_keys": candidate_group_keys,
    }

    if not hits:
        return GroupedAnimeMatch(
            decision="leave",
            reason="no_exact_anime_ids_external_id_match",
            **result_kwargs,
        )
    if not mal_ids:
        return GroupedAnimeMatch(
            decision="leave",
            reason="anime_ids_match_has_no_mal_identity",
            **result_kwargs,
        )
    if len(candidate_group_keys) > 1:
        logger.info(
            "grouped_anime_classifier decision=leave reason=conflicting_exact_external_ids "
            "revision=%s digest=%s groups=%s",
            snapshot.revision,
            snapshot.digest,
            ",".join(candidate_group_keys),
        )
        return GroupedAnimeMatch(
            decision="leave",
            reason="conflicting_exact_external_ids",
            **result_kwargs,
        )

    group_key = candidate_group_keys[0]
    group_entries = snapshot.groups[group_key]
    result_kwargs["mapping_keys"] = tuple(
        sorted(entry["mapping_key"] for entry in group_entries),
    )
    result_kwargs["mal_ids"] = tuple(
        sorted({mal_id for entry in group_entries for mal_id in entry["mal_ids"]}),
    )
    result_kwargs["mapping_group_key"] = group_key
    mal_ids = result_kwargs["mal_ids"]

    genres = metadata.get("genres") or []
    if not any(str(genre).casefold() == ANIMATION_GENRE for genre in genres):
        return GroupedAnimeMatch(
            decision="leave",
            reason="tmdb_metadata_is_not_tagged_animation",
            **result_kwargs,
        )

    reason = "exact_external_id_and_animation_genre"
    if len(mal_ids) > 1:
        reason += "_multiple_mal_seasons"
    logger.info(
        "grouped_anime_classifier decision=move reason=%s revision=%s digest=%s "
        "group=%s matched_by=%s",
        reason,
        snapshot.revision,
        snapshot.digest,
        group_key,
        ",".join(result_kwargs["matched_by"]),
    )
    return GroupedAnimeMatch(
        decision="move",
        reason=reason,
        **result_kwargs,
    )


def _tree_items(tv_item: Item) -> list[Item]:
    """Return the parent and all season/episode Items attached to it."""
    seasons = list(
        Season.objects.filter(related_tv__item=tv_item).select_related("item"),
    )
    episodes = list(
        Episode.objects.filter(related_season__related_tv__item=tv_item).select_related(
            "item",
        ),
    )
    return [
        tv_item,
        *(season.item for season in seasons),
        *(episode.item for episode in episodes),
    ]


def _target_collision(items: list[Item]) -> str | None:
    """Return a collision reason before changing any library buckets."""
    for item in items:
        identity = {
            "media_id": item.media_id,
            "source": item.source,
            "media_type": item.media_type,
            "library_media_type": GROUPED_BUCKET,
            "season_number": item.season_number,
            "episode_number": item.episode_number,
        }
        if Item.objects.select_for_update().filter(**identity).exclude(pk=item.pk).first():
            return f"target_bucket_collision_for_item_{item.pk}"
    return None


def _upsert_grouped_link(
    item: Item,
    provider: str,
    provider_media_id: str | None,
    link_metadata: dict[str, Any],
) -> None:
    """Persist a show-level link through the shared race-safe helper."""
    if not provider_media_id:
        return
    external_key = {
        Sources.TMDB.value: "tmdb_id",
        Sources.TVDB.value: "tvdb_id",
        Sources.MAL.value: "mal_id",
    }.get(provider)
    metadata = {
        "source": provider,
        "media_id": str(provider_media_id),
        "media_type": MediaTypes.TV.value,
        "provider_external_ids": (
            {external_key: str(provider_media_id)} if external_key else {}
        ),
    }
    metadata_resolution.upsert_provider_links(
        item=item,
        metadata=metadata,
        provider=provider,
        provider_media_type=MediaTypes.TV.value,
        extra_metadata=link_metadata,
        persistence_mode="required",
    )


@transaction.atomic
def promote_grouped_anime(
    tv_item: Item,
    match: GroupedAnimeMatch,
) -> bool:
    """Promote a TV tree to the anime bucket, preserving all row IDs/history.

    Returns False when the target bucket is occupied by another Item.  No
    partial update is committed in that case.
    """
    if not match.is_grouped_anime:
        return False
    if (
        tv_item.media_type != MediaTypes.TV.value
        or tv_item.source not in GROUPED_PARENT_TYPES
    ):
        return False

    items = _tree_items(tv_item)
    item_ids = sorted({item.pk for item in items})
    locked_items = list(
        Item.objects.select_for_update().filter(pk__in=item_ids).order_by("pk"),
    )
    items_by_id = {item.pk: item for item in locked_items}
    tv_item = items_by_id[tv_item.pk]
    items = [items_by_id[item_id] for item_id in item_ids]
    collision = _target_collision(items)
    if collision:
        return False

    parent_external_ids = dict(tv_item.provider_external_ids or {})
    parent_external_ids.update(
        {
            key: value
            for key, value in (
                ("tmdb_id", match.tmdb_id),
                ("tvdb_id", match.tvdb_id),
                ("imdb_id", match.imdb_id),
            )
            if value
        },
    )
    if len(match.mal_ids) == 1:
        parent_external_ids["mal_id"] = match.mal_ids[0]

    for item in items:
        updates = []
        if item.library_media_type != GROUPED_BUCKET:
            item.library_media_type = GROUPED_BUCKET
            updates.append("library_media_type")
        if item is tv_item and item.provider_external_ids != parent_external_ids:
            item.provider_external_ids = parent_external_ids
            updates.append("provider_external_ids")
        if updates:
            item.save(update_fields=updates)

    link_metadata = {
        "migration": "grouped-anime-classifier",
        "mapping_source": match.mapping_source,
        "mapping_revision": match.mapping_revision,
        "mapping_digest": match.mapping_digest,
        "mapping_group_key": match.mapping_group_key,
        "mapping_keys": list(match.mapping_keys),
    }
    _upsert_grouped_link(tv_item, Sources.TMDB.value, match.tmdb_id, link_metadata)
    _upsert_grouped_link(tv_item, Sources.TVDB.value, match.tvdb_id, link_metadata)
    if len(match.mal_ids) == 1:
        _upsert_grouped_link(tv_item, Sources.MAL.value, match.mal_ids[0], link_metadata)
    logger.info(
        "grouped_anime_promotion result=applied item=%s group=%s revision=%s digest=%s",
        tv_item.pk,
        match.mapping_group_key,
        match.mapping_revision,
        match.mapping_digest,
    )
    return True
