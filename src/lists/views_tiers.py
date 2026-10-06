"""Endpoints behind the Tiers view: move an item between tiers, edit the tiers."""

import json

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from lists.models import CustomList, CustomListItem
from lists.tiers import clean_tiers, resolve_tiers
from lists.views_add_reorder import apply_full_order


def _editable_list(request, list_id):
    """Return the list, or None when the user may not change its tiers."""
    custom_list = get_object_or_404(CustomList, id=list_id)
    if custom_list.is_smart or not custom_list.user_can_edit(request.user):
        return None
    return custom_list


@login_required
@require_POST
def move_tier_item(request, list_id):
    """Put one item in a tier and keep the tier's order as dropped.

    ``item_ids[]`` is the target tier's items in their new order; it sets the
    item's place among them with the same ordering the custom sort uses.
    """
    custom_list = _editable_list(request, list_id)
    if custom_list is None:
        return HttpResponse(status=403)

    tier = request.POST.get("tier", "")
    if tier and tier not in {entry["id"] for entry in resolve_tiers(custom_list)}:
        return HttpResponse(status=400)

    with transaction.atomic():
        moved = CustomListItem.objects.filter(
            custom_list=custom_list,
            item_id=request.POST.get("item_id") or 0,
        ).update(tier=tier)
        if not moved:
            return HttpResponse(status=404)
        item_ids = request.POST.getlist("item_ids[]")
        if item_ids:
            apply_full_order(custom_list, item_ids)
    return HttpResponse(status=204)


@login_required
@require_POST
def save_tiers(request, list_id):
    """Replace the list's tiers; items in a removed tier become Unranked."""
    custom_list = _editable_list(request, list_id)
    if custom_list is None:
        return HttpResponse(status=403)

    try:
        tiers = clean_tiers(json.loads(request.body or b"{}").get("tiers"))
    except (ValueError, AttributeError):
        return HttpResponse(status=400)

    with transaction.atomic():
        custom_list.tiers = tiers
        custom_list.save(update_fields=["tiers"])
        CustomListItem.objects.filter(custom_list=custom_list).exclude(
            tier__in=[entry["id"] for entry in tiers],
        ).update(tier="")
    return JsonResponse({"tiers": tiers})
