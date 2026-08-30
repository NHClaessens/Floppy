from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0033_koreaderaccount_skip_finished_books"),
    ]

    operations = [
        migrations.AlterField(
            model_name="koreaderaccount",
            name="finished_threshold",
            field=models.FloatField(
                default=1.0,
                help_text="Reading progress fraction (0-1) at which a synced book is marked completed",
            ),
        ),
    ]
