import datetime

from django.db import migrations

# Deliberately not expressed as `release_datetime__year__lte=1`: Django
# computes year-lookup bounds by making a naive year-1 datetime aware in the
# *server's local* TIME_ZONE and converting that to UTC. At year 1, zoneinfo
# resolves to each location's historical LMT offset; for zones with a
# positive LMT offset (e.g. Europe/Berlin, Asia/Tokyo) that conversion
# underflows below datetime.min and raises OverflowError, crashing this
# migration for every affected install regardless of whether any row
# actually needs cleanup. Comparing against an already-UTC-aware bound
# sidesteps that local-timezone conversion entirely.
SENTINEL_CUTOFF = datetime.datetime(2, 1, 1, tzinfo=datetime.timezone.utc)


def clear_sentinel_release_datetimes(apps, schema_editor):
    """Clear release_datetime values stuck at the year-1 sentinel.

    These were produced by extract_release_datetime() before it gained a
    lower-bound sanity check, and crash date-rendering template filters with
    OverflowError on servers running a negative UTC offset.
    """
    Item = apps.get_model("app", "Item")
    Item.objects.filter(release_datetime__lt=SENTINEL_CUTOFF).update(
        release_datetime=None,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0170_remove_item_app_item_source_valid_and_more"),
    ]

    operations = [
        migrations.RunPython(
            clear_sentinel_release_datetimes,
            migrations.RunPython.noop,
        ),
    ]
