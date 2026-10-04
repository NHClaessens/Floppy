from django.db import migrations, models


def _column_exists(schema_editor, table_name, column_name):
    """Return True when a database column already exists."""
    connection = schema_editor.connection
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = %s AND column_name = %s",
                [table_name, column_name],
            )
            return cursor.fetchone() is not None
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table_name)
        columns = {getattr(column, "name", column[0]) for column in description}
        return column_name in columns


def _index_exists(schema_editor, table_name, index_name):
    """Return True when the named index already exists on the table."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        try:
            constraints = connection.introspection.get_constraints(cursor, table_name)
        except Exception:  # noqa: BLE001 - introspection quirks shouldn't break migrate
            return False
    return index_name in constraints


class AddFieldIfNotExists(migrations.AddField):
    """Add a field only when the backing column doesn't already exist."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        """Skip the column add when the column is already there."""
        to_model = to_state.apps.get_model(app_label, self.model_name)
        field = to_model._meta.get_field(self.name)
        if _column_exists(schema_editor, to_model._meta.db_table, field.column):
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class AddIndexIfNotExists(migrations.AddIndex):
    """Add an index only when it doesn't already exist."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        """Skip the index add when the index is already there."""
        to_model = to_state.apps.get_model(app_label, self.model_name)
        if _index_exists(schema_editor, to_model._meta.db_table, self.index.name):
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0182_episode_order_archived_and_more"),
    ]

    operations = [
        AddFieldIfNotExists(
            model_name="person",
            name="profile_backfill_fail_count",
            field=models.PositiveIntegerField(default=0),
        ),
        AddFieldIfNotExists(
            model_name="person",
            name="profile_backfill_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        AddFieldIfNotExists(
            model_name="person",
            name="profile_backfill_next_retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        AddFieldIfNotExists(
            model_name="person",
            name="profile_backfill_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="metadatabackfillstate",
            name="field",
            field=models.CharField(
                choices=[
                    ("runtime", "Runtime"),
                    ("genres", "Genres"),
                    ("credits", "Credits"),
                    ("release", "Release Date"),
                    ("discover", "Discover Metadata"),
                    ("game_lengths", "Game Lengths"),
                    ("trakt_popularity", "Trakt Popularity"),
                    ("igdb_ratings", "IGDB Ratings"),
                    ("watch_providers", "Watch Providers"),
                    ("external_ids", "External IDs"),
                    ("studios", "Studios"),
                    ("imdb_match", "IMDB Title Match"),
                ],
                max_length=20,
            ),
        ),
        AddIndexIfNotExists(
            model_name="person",
            index=models.Index(
                fields=["source", "profile_backfill_version"],
                name="app_person_source_5f8272_idx",
            ),
        ),
    ]
