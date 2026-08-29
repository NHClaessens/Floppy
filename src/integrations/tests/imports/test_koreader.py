"""Tests for the KOReader sync importer."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from app.models import Book, Item, MediaTypes, Sources, Status
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError
from integrations.imports.koreader import (
    KoreaderAuthError,
    KoreaderClient,
    KoreaderImporter,
)
from integrations.koreader_links import (
    get_document_hash_for_item,
    normalize_document_hash,
    save_document_link,
)
from integrations.models import KoreaderAccount, KoreaderDocumentLink

DOCUMENT_HASH = "0b229176d4e8db7f6d2b5a4952368d7a"


class KoreaderClientTests(TestCase):
    """Validate KOReader client helpers."""

    def test_password_to_auth_key(self):
        self.assertEqual(
            KoreaderClient.password_to_auth_key("test"),
            "098f6bcd4621d373cade4e832627b4f6",
        )


class KoreaderLinkHelperTests(TestCase):
    """Validate document link helpers."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="koreader-links",
            password="pass",
        )
        self.item = Item.objects.create(
            media_id="123",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Dune",
        )

    def test_normalize_document_hash(self):
        self.assertEqual(
            normalize_document_hash(f"  {DOCUMENT_HASH.upper()}  "),
            DOCUMENT_HASH,
        )
        self.assertIsNone(normalize_document_hash("not-a-hash"))

    def test_save_and_read_document_link(self):
        save_document_link(self.user, self.item, DOCUMENT_HASH)
        self.assertEqual(
            get_document_hash_for_item(self.user, self.item),
            DOCUMENT_HASH,
        )

    def test_clearing_document_link(self):
        save_document_link(self.user, self.item, DOCUMENT_HASH)
        save_document_link(self.user, self.item, "")
        self.assertEqual(get_document_hash_for_item(self.user, self.item), "")


class KoreaderImporterTests(TestCase):
    """Validate KOReader import mapping and discovery."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="koreader-user",
            password="pass",
        )
        KoreaderAccount.objects.create(
            user=self.user,
            server_url="https://kosync.example.com",
            username="reader",
            auth_key=helpers.encrypt(
                KoreaderClient.password_to_auth_key("secret"),
            ),
            create_missing=False,
        )
        self.item = Item.objects.create(
            media_id="42",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Dune",
            number_of_pages=400,
        )

    @patch("integrations.imports.koreader.KoreaderClient.probe_list_support")
    @patch("integrations.imports.koreader.KoreaderClient.get_progress")
    def test_syncs_linked_document_progress(self, mock_progress, mock_probe):
        mock_probe.return_value = False
        mock_progress.return_value = {
            "document": DOCUMENT_HASH,
            "percentage": 0.5,
            "timestamp": 1_700_000_000,
        }
        KoreaderDocumentLink.objects.create(
            user=self.user,
            item=self.item,
            document_hash=DOCUMENT_HASH,
        )
        Book.objects.create(
            user=self.user,
            item=self.item,
            status=Status.PLANNING.value,
            progress=0,
        )

        counts, warnings = KoreaderImporter(self.user).import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        media = Book.objects.get(user=self.user, item=self.item)
        self.assertEqual(media.status, Status.IN_PROGRESS.value)
        self.assertEqual(media.progress, 200)

    @patch("integrations.imports.koreader.KoreaderClient.list_documents")
    @patch("integrations.imports.koreader.KoreaderClient.probe_list_support")
    @patch("integrations.imports.koreader.KoreaderClient.get_progress")
    def test_skips_unlinked_documents_without_metadata(
        self,
        mock_progress,
        mock_probe,
        mock_list,
    ):
        mock_probe.return_value = True
        mock_list.return_value = [
            {
                "document": DOCUMENT_HASH,
                "percentage": 0.4,
                "timestamp": 1_700_000_000,
            },
        ]

        counts, warnings = KoreaderImporter(self.user).import_data()

        self.assertEqual(counts, {})
        self.assertIn("link it on the book track modal", warnings)
        mock_progress.assert_not_called()

    @patch("integrations.imports.koreader.KoreaderClient.list_documents")
    @patch("integrations.imports.koreader.KoreaderClient.probe_list_support")
    def test_auto_match_creates_link_and_updates_existing_book(
        self,
        mock_probe,
        mock_list,
    ):
        mock_probe.return_value = True
        mock_list.return_value = [
            {
                "document": DOCUMENT_HASH,
                "percentage": 0.96,
                "title": "Dune",
                "authors": "Frank Herbert",
                "timestamp": 1_700_000_000,
            },
        ]
        Book.objects.create(
            user=self.user,
            item=self.item,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )

        counts, warnings = KoreaderImporter(self.user).import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        self.assertTrue(
            KoreaderDocumentLink.objects.filter(
                user=self.user,
                document_hash=DOCUMENT_HASH,
                item=self.item,
            ).exists(),
        )
        media = Book.objects.get(user=self.user, item=self.item)
        self.assertEqual(media.status, Status.COMPLETED.value)
        self.assertEqual(media.progress, 400)

    @patch("integrations.imports.koreader.KoreaderClient.probe_list_support")
    @patch("integrations.imports.koreader.KoreaderClient.get_progress")
    def test_marks_account_broken_on_auth_error(self, mock_progress, mock_probe):
        mock_probe.return_value = False
        mock_progress.side_effect = KoreaderAuthError("bad creds")
        KoreaderDocumentLink.objects.create(
            user=self.user,
            item=self.item,
            document_hash=DOCUMENT_HASH,
        )

        with self.assertRaises(MediaImportError):
            KoreaderImporter(self.user).import_data()

        account = KoreaderAccount.objects.get(user=self.user)
        self.assertTrue(account.connection_broken)
        self.assertIn("bad creds", account.last_error_message)


class KoreaderViewTests(TestCase):
    """Validate KOReader connect/disconnect views."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="koreader-view",
            password="pass",
        )
        self.client = Client()
        self.client.force_login(self.user)

    @patch("integrations.views.tasks.import_koreader.delay")
    @patch("integrations.views.KoreaderClient.auth")
    def test_connect_creates_account(self, mock_auth, mock_delay):
        mock_auth.return_value = True
        response = self.client.post(
            "/import/koreader/connect",
            {
                "server_url": "https://kosync.example.com",
                "username": "reader",
                "password": "secret",
                "verify_ssl": "on",
                "frequency": "once",
                "mode": "new",
                "time": "00:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        account = KoreaderAccount.objects.get(user=self.user)
        self.assertEqual(account.username, "reader")
        self.assertTrue(account.verify_ssl)

    def test_disconnect_removes_account_and_links(self):
        item = Item.objects.create(
            media_id="99",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
        )
        KoreaderAccount.objects.create(
            user=self.user,
            server_url="https://kosync.example.com",
            username="reader",
            auth_key=helpers.encrypt("abc"),
        )
        KoreaderDocumentLink.objects.create(
            user=self.user,
            item=item,
            document_hash=DOCUMENT_HASH,
        )

        response = self.client.post("/import/koreader/disconnect")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(KoreaderAccount.objects.filter(user=self.user).exists())
        self.assertFalse(KoreaderDocumentLink.objects.filter(user=self.user).exists())


class KoreaderTrackModalTests(TestCase):
    """Validate KOReader document ID field on book track modal save."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="koreader-track",
            password="pass",
        )
        self.client = Client()
        self.client.force_login(self.user)
        KoreaderAccount.objects.create(
            user=self.user,
            server_url="https://kosync.example.com",
            username="reader",
            auth_key=helpers.encrypt("abc"),
        )
        self.item = Item.objects.create(
            media_id="777",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Foundation",
            number_of_pages=250,
        )
        self.book = Book.objects.create(
            user=self.user,
            item=self.item,
            status=Status.IN_PROGRESS.value,
            progress=50,
        )

    def test_media_save_persists_document_link(self):
        response = self.client.post(
            "/media_save",
            {
                "media_id": self.item.media_id,
                "source": self.item.source,
                "media_type": MediaTypes.BOOK.value,
                "instance_id": self.book.id,
                "status": Status.IN_PROGRESS.value,
                "progress": "50",
                "koreader_document_id": DOCUMENT_HASH,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            KoreaderDocumentLink.objects.filter(
                user=self.user,
                item=self.item,
                document_hash=DOCUMENT_HASH,
            ).exists(),
        )
