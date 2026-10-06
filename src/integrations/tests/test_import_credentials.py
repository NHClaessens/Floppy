"""Background jobs must see the user's personal provider keys (#1488).

A Celery task runs no middleware, so the importer only resolves a personal
Client ID/API key when the task itself publishes the user.
"""

from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from app.providers import credentials
from integrations.models import LastFMAccount
from integrations.tasks import _lastfm
from integrations.tasks._media_imports import import_media


@override_settings(SIMKL_ID="", SIMKL_SECRET="", LASTFM_API_KEY="")
class PersonalCredentialsInBackgroundJobsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="personal-keys")
        credentials.set_user(
            "simkl",
            self.user,
            {"client_id": "personal-id", "client_secret": "personal-secret"},
        )
        credentials.set_user("lastfm", self.user, {"api_key": "personal-lastfm"})

    def test_import_media_resolves_the_users_personal_keys(self):
        seen = {}

        def importer(identifier, user, mode, **kwargs):
            seen["client_id"] = credentials.get("simkl", "client_id")
            return {"created": 0, "updated": 0, "skipped": 0}, ""

        with (
            patch("app.mixins.disable_fetch_releases"),
            patch("integrations.tasks._media_imports.import_progress.tracking"),
        ):
            import_media(importer, None, self.user.id, "new")

        self.assertEqual(seen["client_id"], "personal-id")
        # The scope ends with the task: nothing leaks into the next one.
        self.assertEqual(credentials.get("simkl", "client_id"), "")

    def test_lastfm_poll_resolves_the_users_personal_key(self):
        LastFMAccount.objects.create(user=self.user, lastfm_username="listener")
        seen = {}

        def sync(account):
            seen["api_key"] = credentials.get("lastfm", "api_key")
            return {"status": "success", "message": "ok"}

        with (
            patch.object(LastFMAccount, "is_connected", True),
            patch.object(_lastfm, "_run_incremental_lastfm_sync", sync),
        ):
            _lastfm.poll_lastfm_for_user(self.user.id)

        self.assertEqual(seen["api_key"], "personal-lastfm")

    def test_lastfm_history_import_resolves_the_users_personal_key(self):
        seen = {}

        def chunk(user_id, reset, import_run_id):
            seen["api_key"] = credentials.get("lastfm", "api_key")
            return {}

        with patch.object(_lastfm, "_import_lastfm_history_chunk", Mock(side_effect=chunk)):
            _lastfm.import_lastfm_history(self.user.id)

        self.assertEqual(seen["api_key"], "personal-lastfm")
