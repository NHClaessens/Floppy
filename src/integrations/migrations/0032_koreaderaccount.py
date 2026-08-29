# Generated manually for KOReader sync integration

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0170_remove_item_app_item_source_valid_and_more"),
        ("integrations", "0031_backfill_collectionsourcestate_instance"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="KoreaderAccount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "server_url",
                    models.URLField(help_text="KOReader sync server URL"),
                ),
                ("username", models.CharField(max_length=150)),
                (
                    "auth_key",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Encrypted MD5 hash of the KOReader sync password",
                    ),
                ),
                (
                    "verify_ssl",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "Verify TLS certificates when connecting to the sync server"
                        ),
                    ),
                ),
                (
                    "create_missing",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "Create Floppy book entries when KOReader documents "
                            "match providers"
                        ),
                    ),
                ),
                (
                    "finished_threshold",
                    models.FloatField(
                        default=0.95,
                        help_text=(
                            "Reading progress fraction (0-1) at which a book is marked read"
                        ),
                    ),
                ),
                ("supports_document_list", models.BooleanField(blank=True, null=True)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("connection_broken", models.BooleanField(default=False)),
                ("last_error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="koreader_account",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "KOReader account",
                "verbose_name_plural": "KOReader accounts",
            },
        ),
        migrations.CreateModel(
            name="KoreaderDocumentLink",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("document_hash", models.CharField(db_index=True, max_length=32)),
                ("linked_at", models.DateTimeField(auto_now_add=True)),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="koreader_document_links",
                        to="app.item",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="koreader_document_links",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "KOReader document link",
                "verbose_name_plural": "KOReader document links",
            },
        ),
        migrations.AddConstraint(
            model_name="koreaderdocumentlink",
            constraint=models.UniqueConstraint(
                fields=("user", "document_hash"),
                name="integrations_koreaderdocumentlink_unique_user_hash",
            ),
        ),
        migrations.AddConstraint(
            model_name="koreaderdocumentlink",
            constraint=models.UniqueConstraint(
                fields=("user", "item"),
                name="integrations_koreaderdocumentlink_unique_user_item",
            ),
        ),
    ]
