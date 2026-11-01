"""Schließt beendete Veranstaltungen und öffnet Taunus spielt für bekannte
Personen.

Beim Deployment werden zwei einmalige Konfigurationen mitgegeben:

1. Alle Veranstaltungen, deren Tage vollständig in der Vergangenheit liegen
   (z. B. die Sommerspiel-Veranstaltung), erhalten den automatischen Schreibschutz
   (auto_readonly) und sind damit readonly - einsehbar, aber nicht mehr
   veränderbar. Die Orga kann den Schreibschutz pro Veranstaltung über die
   Bearbeitung jederzeit wieder aufheben.

2. Die „Taunus spielt“-Veranstaltungen erlauben bekannten Personen die
   Anmeldung ohne Zugangsschlüssel (allow_known_persons_signup): Wer schonmal
   bei einer (beendeten) Veranstaltung dabei war, kommt mit E-Mail-Code
   hinein, ohne das Zugangspasswort zu kennen.

Beide Auswahlen sind bewusst datums- bzw. titelbasiert und laufen auf
Umgebungen ohne passende Veranstaltungen ins Leere (No-op).
"""

from django.utils import timezone
from django.db import migrations


def close_finished_events_and_enable_known_persons(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    today = timezone.localdate()
    finished = (
        Event.objects.filter(days__date__lt=today)
        .exclude(days__date__gte=today)
        .distinct()
    )
    finished.update(auto_readonly=True)
    Event.objects.filter(title__icontains="taunus spielt").update(
        allow_known_persons_signup=True
    )


def unclose_and_disable(apps, schema_editor):
    # Zeitabhängige Auswahl: Beim Rückwärtslaufen können die ursprünglichen
    # Zustände nicht zuverlässig rekonstruiert werden. Alle betroffenen Flags
    # existierten vor dieser Migration nicht, daher ist ein Zurücksetzen auf
    # False ausreichend.
    Event = apps.get_model("events", "Event")
    Event.objects.filter(title__icontains="taunus spielt").update(
        allow_known_persons_signup=False
    )
    Event.objects.filter(auto_readonly=True).update(auto_readonly=False)


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0025_event_allow_known_persons_signup_event_auto_readonly"),
    ]

    operations = [
        migrations.RunPython(
            close_finished_events_and_enable_known_persons,
            unclose_and_disable,
        ),
    ]
