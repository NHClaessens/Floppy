from django.db import migrations, models


def _column_exists(schema_editor, table_name, column_name):
    """Return True when the backing column already exists."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(
            cursor,
            table_name,
        )
        columns = {getattr(column, "name", column[0]) for column in description}
    return column_name in columns


class AddFieldIfNotExists(migrations.AddField):
    """Add a field only when the backing column doesn't already exist."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        """Skip the ALTER when the column is already there."""
        to_model = to_state.apps.get_model(app_label, self.model_name)
        field = to_model._meta.get_field(self.name)  # noqa: SLF001
        if _column_exists(schema_editor, to_model._meta.db_table, field.column):  # noqa: SLF001
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0180_item_metadata_refreshed_at"),
    ]

    operations = [
        AddFieldIfNotExists(
            model_name="item",
            name="provider_episode_count",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Total episode count reported by the metadata provider",
                null=True,
            ),
        ),
    ]
