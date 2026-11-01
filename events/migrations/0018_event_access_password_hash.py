from django.contrib.auth.hashers import make_password
from django.db import migrations, models


def hash_existing_access_passwords(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    for event in Event.objects.exclude(access_password="").iterator():
        event.access_password_hash = make_password(event.access_password)
        event.save(update_fields=["access_password_hash"])


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0017_alter_potluckitem_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="access_password_hash",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Wird nur gehasht gespeichert (django.contrib.auth.hashers), "
                    "nicht im Klartext. Leer lassen, um die Veranstaltung ohne "
                    "Passwort zugänglich zu machen."
                ),
                max_length=128,
                verbose_name="Zugangspasswort (gehasht)",
            ),
        ),
        migrations.RunPython(
            hash_existing_access_passwords,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="event",
            name="access_password",
        ),
    ]
