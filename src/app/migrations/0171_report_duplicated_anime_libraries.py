"""Report anime tracked in both the Anime and TV libraries (discussion #967).

Detection only. It touches no rows and calls no provider, so it can never fail
an upgrade: `entrypoint.sh` gives up after five migration attempts, and a merge
here would need TMDB/TVDB metadata. The merge itself runs later, in the
"Repair duplicated anime libraries" Celery task, which retries safely.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

NON_ANIME_TV_BUCKETS = ("", "tv")


def report_duplicates(apps, schema_editor):
    """Log how many shows are tracked in both libraries."""
    Anime = apps.get_model("app", "Anime")
    TV = apps.get_model("app", "TV")
    ItemProviderLink = apps.get_model("app", "ItemProviderLink")

    try:
        links = ItemProviderLink.objects.filter(
            provider__in=("tmdb", "tvdb"),
            provider_media_type="tv",
            item__media_type="anime",
        ).values_list("item_id", "provider", "provider_media_id")

        identities = {}
        for item_id, provider, provider_media_id in links:
            identities.setdefault(item_id, []).append((provider, provider_media_id))
        if not identities:
            return

        anime_owners = dict(
            Anime.objects.filter(
                item_id__in=identities,
                migrated_to_item__isnull=True,
            ).values_list("item_id", "user_id"),
        )

        duplicates = 0
        for item_id, user_id in anime_owners.items():
            for provider, provider_media_id in identities.get(item_id, []):
                if TV.objects.filter(
                    user_id=user_id,
                    item__source=provider,
                    item__media_id=provider_media_id,
                    item__media_type="tv",
                    item__library_media_type__in=NON_ANIME_TV_BUCKETS,
                ).exists():
                    duplicates += 1
                    break

        if duplicates:
            logger.warning(
                "%s show(s) are tracked in both the Anime and TV libraries. "
                "The 'Repair duplicated anime libraries' task will fold them "
                "back together; nothing was changed here.",
                duplicates,
            )
    except Exception:  # pragma: no cover - reporting must never block an upgrade
        logger.warning("Could not survey duplicated anime libraries", exc_info=True)


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0170_remove_item_app_item_source_valid_and_more"),
    ]

    operations = [
        migrations.RunPython(report_duplicates, migrations.RunPython.noop),
    ]
