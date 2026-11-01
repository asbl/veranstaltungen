from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0020_eventvisit_broughtperson_created_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="potluckitem",
            name="unit",
            field=models.CharField(
                blank=True,
                help_text="Optional, z. B. Flaschen, Liter oder Packungen.",
                max_length=50,
                verbose_name="Einheit",
            ),
        ),
    ]
