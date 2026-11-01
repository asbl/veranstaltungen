import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0019_potluckitem_variable_per_persons_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="broughtperson",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.CreateModel(
            name="EventVisit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_visited_at", models.DateTimeField()),
                (
                    "event",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="visits", to="events.event"),
                ),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_visits", to=settings.AUTH_USER_MODEL),
                ),
            ],
            options={
                "verbose_name": "Veranstaltungsbesuch",
                "verbose_name_plural": "Veranstaltungsbesuche",
                "unique_together": {("event", "user")},
            },
        ),
    ]
