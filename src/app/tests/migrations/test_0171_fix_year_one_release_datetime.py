import datetime
import importlib

from django.test import TestCase, override_settings

from app.models import Item, MediaTypes, Sources

migration = importlib.import_module(
    "app.migrations.0171_fix_year_one_release_datetime",
)


class _RealAppsRegistry:
    """Minimal stand-in for the historical `apps` migrations pass in."""

    def get_model(self, app_label, model_name):
        del app_label
        return {"Item": Item}[model_name]


def _item(release_datetime):
    return Item.objects.create(
        media_id="1",
        source=Sources.TMDB.value,
        media_type=MediaTypes.MOVIE.value,
        title="Sentinel",
        release_datetime=release_datetime,
    )


class FixYearOneReleaseDatetimeMigration(TestCase):
    """Regression test for issue #1051.

    A server whose TIME_ZONE has a positive LMT offset (e.g. Europe/Berlin,
    Asia/Tokyo) made this migration raise OverflowError on every run, empty
    database included, because `__year__lte=1` asked Django to convert a
    year-1 bound through the local timezone into UTC.
    """

    @override_settings(TIME_ZONE="Europe/Berlin")
    def test_does_not_crash_under_a_positive_lmt_offset_timezone(self):
        migration.clear_sentinel_release_datetimes(_RealAppsRegistry(), None)

    @override_settings(TIME_ZONE="Europe/Berlin")
    def test_clears_the_year_one_sentinel(self):
        sentinel = _item(datetime.datetime(1, 6, 1, tzinfo=datetime.timezone.utc))

        migration.clear_sentinel_release_datetimes(_RealAppsRegistry(), None)

        sentinel.refresh_from_db()
        self.assertIsNone(sentinel.release_datetime)

    @override_settings(TIME_ZONE="Europe/Berlin")
    def test_leaves_valid_release_datetimes_alone(self):
        valid = _item(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))

        migration.clear_sentinel_release_datetimes(_RealAppsRegistry(), None)

        valid.refresh_from_db()
        self.assertEqual(valid.release_datetime, datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
