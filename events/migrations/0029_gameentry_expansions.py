from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0028_merge_cake_list_cleanup_taunus_notes"),
    ]

    operations = [
        migrations.AddField(
            model_name="gameentry",
            name="expansions",
            field=models.TextField(
                blank=True,
                db_default="",
                help_text="Welche Erweiterungen bringst du mit? (z. B. „Seefahrer“)",
                verbose_name="Erweiterungen",
            ),
        ),
    ]
