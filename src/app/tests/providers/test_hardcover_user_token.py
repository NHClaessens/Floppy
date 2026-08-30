from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from app import metadata_utils
from app.models import Sources
from app.providers import hardcover, services
from integrations.imports.helpers import encrypt


class HardcoverUserTokenTests(TestCase):
    """Coverage for per-user Hardcover API token resolution (#937)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="hardcover-user",
            password="12345",
        )

    @override_settings(HARDCOVER_API="instance-default-token")
    def test_falls_back_to_instance_default_when_no_user(self):
        self.assertEqual(
            hardcover._authorization_header(),
            "Bearer instance-default-token",
        )

    @override_settings(HARDCOVER_API="instance-default-token")
    def test_falls_back_to_instance_default_when_user_has_no_key(self):
        self.assertEqual(
            hardcover._authorization_header(self.user),
            "Bearer instance-default-token",
        )

    @override_settings(HARDCOVER_API="instance-default-token")
    def test_uses_users_own_key_when_set(self):
        self.user.hardcover_api_key = encrypt("personal-token")
        self.user.save(update_fields=["hardcover_api_key"])

        self.assertEqual(
            hardcover._authorization_header(self.user),
            "Bearer personal-token",
        )

    @override_settings(HARDCOVER_API="instance-default-token")
    def test_different_users_get_different_tokens(self):
        other_user = get_user_model().objects.create_user(
            username="other-hardcover-user",
            password="12345",
        )
        self.user.hardcover_api_key = encrypt("token-a")
        self.user.save(update_fields=["hardcover_api_key"])
        other_user.hardcover_api_key = encrypt("token-b")
        other_user.save(update_fields=["hardcover_api_key"])

        self.assertEqual(
            hardcover._authorization_header(self.user),
            "Bearer token-a",
        )
        self.assertEqual(
            hardcover._authorization_header(other_user),
            "Bearer token-b",
        )

    @patch("app.providers.hardcover.services.api_request")
    def test_search_forwards_user_token_to_request_headers(self, mock_api_request):
        self.user.hardcover_api_key = encrypt("personal-token")
        self.user.save(update_fields=["hardcover_api_key"])
        mock_api_request.return_value = {"data": {"search": {"results": None}}}

        hardcover.cache.delete("search_hardcover_book_user-token-query_1")
        hardcover.search("user-token-query", 1, user=self.user)

        headers = mock_api_request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer personal-token")


class HardcoverUnconfiguredTests(TestCase):
    """Hardcover ships no default token, so it must degrade cleanly (#1025).

    The bundled token authenticated every Floppy install as one Hardcover
    account, whose per-account daily quota was permanently exhausted.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="no-hardcover",
            password="12345",
        )

    @override_settings(HARDCOVER_API="")
    def test_enabled_is_false_without_an_instance_token(self):
        self.assertFalse(hardcover.enabled())

    @override_settings(HARDCOVER_API="")
    def test_a_request_fails_before_the_network_call(self):
        """An empty Authorization header would spend a request earning a 401."""
        with self.assertRaises(services.ProviderAPIError) as caught:
            hardcover._authorization_header(self.user)

        self.assertIn("HARDCOVER_API", str(caught.exception))

    @override_settings(HARDCOVER_API="")
    def test_a_personal_token_still_works(self):
        self.user.hardcover_api_key = encrypt("personal-token")

        self.assertEqual(
            hardcover._authorization_header(self.user),
            "Bearer personal-token",
        )

    @override_settings(HARDCOVER_API="")
    def test_backfills_skip_hardcover_without_an_instance_token(self):
        """Background jobs have no user, so they must not queue dead work."""
        sources = metadata_utils.backfill_sources(
            (Sources.TMDB.value, Sources.HARDCOVER.value, Sources.OPENLIBRARY.value),
        )

        self.assertNotIn(Sources.HARDCOVER.value, sources)
        self.assertIn(Sources.OPENLIBRARY.value, sources)

    @override_settings(HARDCOVER_API="instance-token")
    def test_backfills_keep_hardcover_when_configured(self):
        sources = metadata_utils.backfill_sources(
            (Sources.TMDB.value, Sources.HARDCOVER.value),
        )

        self.assertIn(Sources.HARDCOVER.value, sources)
