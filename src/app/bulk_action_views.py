"""HTTP handlers for item-based bulk actions."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from app import bulk_actions
from app.collection_views import (
    _SEASON_OR_SHOW_MEDIA_TYPES,
    _expand_collection_entry_to_episodes,
)
from app.models import CollectionEntry, Item, Status


def _missing_item_ids_response():
    return JsonResponse(
        {"success": False, "error": "At least one item is required."},
        status=400,
    )


@login_required
@require_POST
def bulk_status_update(request):
    """Update the selected user's item-backed media trackers."""
    item_ids = bulk_actions.posted_item_ids(request.POST)
    if not item_ids:
        return _missing_item_ids_response()

    status = request.POST.get("status", "")
    valid_statuses = {value for value, _label in Status.choices}
    if status not in valid_statuses:
        return JsonResponse(
            {"success": False, "error": "Invalid status."},
            status=400,
        )

    result = bulk_actions.apply_bulk_status(request.user, item_ids, status)
    result["success"] = True
    result["message"] = (
        f"Updated {result['updated'] + result['created']} item(s)."
        + (f" {result['created']} tracker(s) created." if result["created"] else "")
        + (f" {result['skipped']} skipped." if result["skipped"] else "")
    )
    return JsonResponse(result)


@login_required
@require_POST
def bulk_collection_quick_add(request):
    """Quick-add selected Items to the user's collection."""
    item_ids = bulk_actions.posted_item_ids(request.POST)
    if not item_ids:
        return _missing_item_ids_response()

    items_by_id = Item.objects.in_bulk(item_ids)
    result = {"created": 0, "already_present": 0, "skipped": 0}
    result["skipped"] = len(item_ids) - len(items_by_id)

    ordinary_items = [
        item
        for item_id, item in items_by_id.items()
        if item_id in item_ids and item.media_type not in _SEASON_OR_SHOW_MEDIA_TYPES
    ]
    existing_ids = set(
        CollectionEntry.objects.filter(
            user=request.user,
            item_id__in=[item.id for item in ordinary_items],
        ).values_list("item_id", flat=True),
    )
    to_create = [
        CollectionEntry(user=request.user, item=item)
        for item in ordinary_items
        if item.id not in existing_ids
    ]
    if to_create:
        CollectionEntry.objects.bulk_create(to_create)
    result["created"] += len(to_create)
    result["already_present"] += len(ordinary_items) - len(to_create)

    for item_id in item_ids:
        item = items_by_id.get(item_id)
        if item is None or item.media_type not in _SEASON_OR_SHOW_MEDIA_TYPES:
            continue
        try:
            with transaction.atomic():
                created_entries, skipped_count = _expand_collection_entry_to_episodes(
                    request.user,
                    item,
                    cleaned_data={},
                )
        except Exception:
            result["skipped"] += 1
            continue
        result["created"] += len(created_entries)
        result["already_present"] += skipped_count

    result["success"] = True
    result["message"] = (
        f"Added {result['created']} item(s) to collection."
        + (f" {result['already_present']} already present." if result["already_present"] else "")
        + (f" {result['skipped']} skipped." if result["skipped"] else "")
    )
    return JsonResponse(result)
