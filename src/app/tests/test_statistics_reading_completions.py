import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import Book, Item, MediaTypes, Sources, Status
from app.statistics_cache import get_statistics_data


class ReadingCompletionsTests(TestCase):
    """Issue #1379: "Books Finished" must count finished books, not reading days."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reader", password="password123"
        )

    def _book(self, media_id, status, start, end, pages=300, progress=None):
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title=f"Book {media_id}",
            number_of_pages=pages,
        )
        tz = timezone.get_current_timezone()
        return Book.objects.create(
            user=self.user,
            item=item,
            status=status,
            progress=pages if progress is None else progress,
            start_date=datetime.datetime(*start, 12, 0, tzinfo=tz),
            end_date=datetime.datetime(*end, 12, 0, tzinfo=tz),
        )

    def test_finished_counts_books_not_reading_days(self):
        # One book read over 30 days in 2023, one read in a single day in 2024,
        # one still in progress (must not count as finished).
        self._book("b1", Status.COMPLETED.value, (2023, 1, 1), (2023, 1, 30))
        self._book("b2", Status.COMPLETED.value, (2024, 5, 4), (2024, 5, 4))
        self._book("b3", Status.IN_PROGRESS.value, (2024, 6, 1), (2024, 6, 10), progress=100)

        data = get_statistics_data(self.user, start_date=None, end_date=None)
        book = data["book_consumption"]

        by_year = book["completion_charts"]["by_year"]
        self.assertEqual(by_year["labels"], ["2023", "2024"])
        self.assertEqual(by_year["datasets"][0]["data"], [1, 1])
        self.assertEqual(book["completions"]["total"], 2)
