from datetime import UTC, datetime
from unittest.mock import MagicMock

from django.test import TestCase, override_settings

from users.models import DateFormatChoices
from users.templatetags import user_tags


class UserDateFormatOverflowTests(TestCase):
    """user_date_format/time/datetime must not raise on the year-1 sentinel."""

    def setUp(self):
        self.user = MagicMock()
        self.user.date_format = DateFormatChoices.ISO_8601

    @override_settings(TIME_ZONE="America/New_York")
    def test_user_date_format_handles_year_one_sentinel(self):
        """A negative UTC offset underflows below datetime.MINYEAR for year 1.

        The filter must fall back to the default format instead of raising.
        """
        sentinel = datetime(1, 1, 1, tzinfo=UTC)
        result = user_tags.user_date_format(sentinel, self.user)
        self.assertIsInstance(result, str)
        self.assertTrue(result)

    @override_settings(TIME_ZONE="America/New_York")
    def test_user_time_format_handles_year_one_sentinel(self):
        sentinel = datetime(1, 1, 1, tzinfo=UTC)
        result = user_tags.user_time_format(sentinel, self.user)
        self.assertIsInstance(result, str)
        self.assertTrue(result)

    @override_settings(TIME_ZONE="America/New_York")
    def test_user_datetime_format_handles_year_one_sentinel(self):
        sentinel = datetime(1, 1, 1, tzinfo=UTC)
        result = user_tags.user_datetime_format(sentinel, self.user)
        self.assertIsInstance(result, str)
        self.assertTrue(result)
