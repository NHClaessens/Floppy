from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, tag
from django.utils import timezone

from app.models import (
    TV,
    MediaTypes,
    Movie,
    Status,
)
from integrations.imports import (
    imdb,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


@tag("network")
class ImportIMDB(TestCase):
    """Test importing media from IMDB CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_imdb.csv").open("rb") as file:
            self.import_results = imdb.importer(file, self.user, "new")

    def test_import_imdb_csv(self):
        """Test importing movies and TV shows from IMDB CSV."""
        imported_counts, warnings = self.import_results

        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 5)
        self.assertEqual(imported_counts[MediaTypes.TV.value], 2)

        self.assertIn(
            "The Last of Us: Unsupported title type 'Video Game' - skipped",
            warnings,
        )

        movie_1 = Movie.objects.get(item__title="The Shawshank Redemption")
        self.assertEqual(movie_1.score, 9)
        self.assertEqual(movie_1.status, Status.COMPLETED.value)
        self.assertEqual(movie_1.progress, 1)
        self.assertEqual(
            movie_1.end_date,
            datetime(2025, 2, 3, tzinfo=timezone.get_current_timezone()),
        )

        game_of_thrones = TV.objects.get(item__title="Game of Thrones")
        self.assertEqual(game_of_thrones.status, Status.PLANNING.value)

    def test_extract_imdb_id(self):
        """Test IMDB ID extraction and formatting."""
        importer_instance = imdb.IMDBImporter(None, self.user, "new")

        self.assertEqual(
            importer_instance._extract_imdb_id({"Const": "tt0111161"}),
            "tt0111161",
        )
        self.assertEqual(
            importer_instance._extract_imdb_id({"Const": "0111161"}),
            "tt0111161",
        )
        self.assertIsNone(importer_instance._extract_imdb_id({"Const": ""}))
        self.assertIsNone(importer_instance._extract_imdb_id({"Const": "invalid"}))

    def test_parse_rating(self):
        """Test rating parsing."""
        importer_instance = imdb.IMDBImporter(None, self.user, "new")

        # Valid ratings
        self.assertEqual(importer_instance._parse_rating("8.5"), 8.5)
        self.assertEqual(importer_instance._parse_rating("10"), 10.0)
        self.assertEqual(importer_instance._parse_rating("1"), 1.0)

        self.assertIsNone(importer_instance._parse_rating(""))
        self.assertIsNone(importer_instance._parse_rating("invalid"))
        self.assertIsNone(importer_instance._parse_rating("11"))
        self.assertIsNone(importer_instance._parse_rating("0"))

    def test_parse_date_rated(self):
        """Test date parsing."""
        importer_instance = imdb.IMDBImporter(None, self.user, "new")

        # Valid date
        parsed_date = importer_instance._parse_date("2023-01-15")
        self.assertEqual(parsed_date.date(), datetime(2023, 1, 15, tzinfo=UTC).date())

        self.assertIsNone(importer_instance._parse_date(""))
        self.assertIsNone(importer_instance._parse_date("invalid-date"))

    def test_is_supported_type(self):
        """Test title type support checking."""
        importer_instance = imdb.IMDBImporter(None, self.user, "new")
        type_tests = {
            ("Movie", True),
            ("TV Series", True),
            ("Short", True),
            ("TV Mini Series", True),
            ("TV Movie", True),
            ("TV Special", True),
            ("Video", True),
            ("TV Episode", False),
            ("TV Short", False),
            ("Video Game", False),
            ("Music Video", False),
            ("Podcast Series", False),
            ("Podcast Episode", False),
        }

        for media_type, result in type_tests:
            self.assertEqual(importer_instance._is_supported_type(media_type), result)

    @patch("app.providers.tmdb.find")
    def test_lookup_in_tmdb_not_found(self, mock_tmdb_find):
        """Test TMDB lookup when no results are found."""
        mock_tmdb_find.return_value = {}

        importer_instance = imdb.IMDBImporter(None, self.user, "new")
        result = importer_instance._lookup_in_tmdb("tt9999999", "movie")

        self.assertIsNone(result)

    def test_duplicate_handling(self):
        """Test handling of duplicate IMDB entries that map to same TMDB ID."""
        imported_counts, warnings = self.import_results

        # There are six movies in the test CSV, one of them is a duplicate
        # The test CSV file contains a duplicate of The Dark Knight
        self.assertEqual(imported_counts.get(MediaTypes.MOVIE.value, 0), 5)

        self.assertIn("They were matched to the same TMDB ID 155", warnings)


class IMDBStreamingTests(TestCase):
    """Exercise local import semantics without a live provider."""

    def setUp(self):
        """Create a user and resolve test identifiers deterministically."""
        self.user = get_user_model().objects.create_user(username="stream-imdb")
        patcher = patch.object(
            imdb.IMDBImporter,
            "_lookup_in_tmdb",
            side_effect=lambda identifier, _: {
                "media_id": int(identifier[2:]),
                "title": "Resolved",
                "image": "",
                "media_type": MediaTypes.MOVIE,
            },
        )
        self.lookup = patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, rows):
        """Build a valid CSV with a multiline title."""
        return BytesIO(("Const,Title,Title Type,Created,Your Rating\n" + rows).encode())

    def test_duplicate_validation_precedes_overwrite(self):
        """Duplicates spanning the file never delete existing tracking."""
        imdb.importer(
            self.upload("tt1,Original,Movie,2025-01-01,8\n"), self.user, "new"
        )
        counts, warnings = imdb.importer(
            self.upload(
                'tt1,"First\nTitle",Movie,2025-01-01,9\n'
                "tt2,Other,Movie,2025-01-01,7\n"
                "tt1,Duplicate,Movie,2025-01-01,10\n",
            ),
            self.user,
            "overwrite",
        )
        self.assertEqual(counts, {MediaTypes.MOVIE: 1})
        self.assertIn("First\nTitle", warnings)
        self.assertEqual(Movie.objects.get(item__media_id="1").score, 8)
        self.assertEqual(self.lookup.call_count, 4)

    def test_late_invalid_encoding_preserves_existing_media(self):
        """An invalid suffix cannot trigger partial overwrite writes."""
        imdb.importer(
            self.upload("tt1,Original,Movie,2025-01-01,8\n"), self.user, "new"
        )
        file = self.upload("tt1,Replacement,Movie,2025-01-01,9\n")
        file.seek(0, 2)
        file.write(b"\xff")
        file.seek(0)
        with self.assertRaises(imdb.MediaImportError):
            imdb.importer(file, self.user, "overwrite")
        self.assertEqual(Movie.objects.get(item__media_id="1").score, 8)

    def test_batches_preserve_history_and_new_mode(self):
        """Multiple persistence batches retain row counts and history."""
        rows = "".join(f"tt{i},Title,Movie,2025-01-01,8\n" for i in range(1, 253))
        counts, warnings = imdb.importer(self.upload(rows), self.user, "new")
        self.assertEqual(counts, {MediaTypes.MOVIE: 252})
        self.assertIsNone(warnings)
        self.assertEqual(Movie.objects.count(), 252)
        self.assertEqual(Movie.history.count(), 252)
        counts, _ = imdb.importer(self.upload(rows), self.user, "new")
        self.assertEqual(counts, {})
