from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0021_potluckitem_unit")]

    operations = [
        migrations.AddField(
            model_name="gameentry",
            name="bgg_categories",
            field=models.JSONField(blank=True, default=list, verbose_name="BGG-Kategorien"),
        ),
        migrations.AddField(
            model_name="gameentry",
            name="bgg_description",
            field=models.TextField(blank=True, verbose_name="BGG-Beschreibung"),
        ),
        migrations.AddField(
            model_name="gameentry",
            name="bgg_mechanics",
            field=models.JSONField(blank=True, default=list, verbose_name="BGG-Mechanismen"),
        ),
        migrations.AddField(
            model_name="gameentry",
            name="bgg_rating",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=4, null=True, verbose_name="BGG-Wertung"),
        ),
    ]
