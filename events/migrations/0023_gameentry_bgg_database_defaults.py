from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0022_gameentry_bgg_details")]

    operations = [
        migrations.AlterField(
            model_name="gameentry",
            name="bgg_categories",
            field=models.JSONField(blank=True, db_default=[], default=list, verbose_name="BGG-Kategorien"),
        ),
        migrations.AlterField(
            model_name="gameentry",
            name="bgg_description",
            field=models.TextField(blank=True, db_default="", verbose_name="BGG-Beschreibung"),
        ),
        migrations.AlterField(
            model_name="gameentry",
            name="bgg_mechanics",
            field=models.JSONField(blank=True, db_default=[], default=list, verbose_name="BGG-Mechanismen"),
        ),
    ]
