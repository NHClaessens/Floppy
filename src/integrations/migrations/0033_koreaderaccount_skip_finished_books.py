from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0032_koreaderaccount"),
    ]

    operations = [
        migrations.AddField(
            model_name="koreaderaccount",
            name="skip_finished_books",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Skip progress fetches for books already marked completed in Floppy"
                ),
            ),
        ),
    ]
