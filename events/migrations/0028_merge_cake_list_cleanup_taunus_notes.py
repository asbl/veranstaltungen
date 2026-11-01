"""Führt die Kuchenliste in die Mitbringliste und bereinigt die Notizen von
„Taunus spielt 26“.

1. Alle Potluck-Items der Kategorie „cake“ (Kuchenliste) werden zur Kategorie
   „bring“ (Mitbringliste) verschoben; die Kuchenliste als eigene Liste entfällt.

2. Für „Taunus spielt 26“ werden die damaligen Discord-Stand-Notizen der
   Orga-Aufgaben entfernt. Nur die Informationen, die für 2026 wieder relevant
   sind (Kassen, Pressekontakt, Pegasus, Catan-Turnier), bleiben stehen.
   Gleiches gilt für den entsprechenden Satz in der Beschreibung.

3. Die Mitbringliste von „Taunus spielt 26“ verliert die Vorjahres-Hinweise
   „Bisher mitgebracht von …“; Becher, Kaffeemaschine und Kannen entfallen.

Die Auswahlen sind titelbasiert bzw. am Notiz-Präfix erkennbar und laufen auf
Umgebungen ohne passende Daten ins Leere (No-op). Ein Rückwärtslaufen kann die
gelöschten Inhalte nicht wiederherstellen; die Bereinigung ist bewusst
irreversibel.
"""

from django.db import migrations, models

TAUNUS_TITLE = "Taunus spielt 26"
DISCORD_PREFIX = "Bisheriger Discord-Stand (für 2026 erneut bestätigen): "
BROUGHT_PREFIX = "Bisher mitgebracht von "
DESCRIPTION_OLD = "Die Aufgaben sind für 2026 neu zu bestätigen; Notizen enthalten den bisherigen Discord-Stand."
DESCRIPTION_NEW = "Die Aufgaben sind für 2026 neu zu bestätigen."
# Notizen, die für 2026 wieder relevant sind; alle anderen vom
# prepare_taunus26-Skript gesetzten Notizen entfallen.
KEPT_NOTES = {
    "Presse einladen": "Pressekontakt des Vorjahres einladen.",
    "Pegasus anschreiben": "Pegasus stellte Spiele.",
    "Kassen für Flohmarkt, Essen und Eintritt organisieren": "Kassen wurden vom Orga-Team mitgebracht (bitte prüfen).",
    "Catan-Turnier organisieren": "Für 2026: Turnierleitung, Anmeldung, Regeln, Material, Startzeit und Teilnehmerzahl klären.",
}
DISCARDED_POTLUCK_TITLES = ("Becher", "Kaffeemaschine", "Kannen")


def merge_cake_into_bring(apps, schema_editor):
    PotluckItem = apps.get_model("events", "PotluckItem")
    PotluckItem.objects.filter(category="cake").update(category="bring")


def cleanup_taunus_data(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    PlanningItem = apps.get_model("events", "PlanningItem")
    PotluckItem = apps.get_model("events", "PotluckItem")
    event = Event.objects.filter(title=TAUNUS_TITLE).first()
    if event is None:
        return
    if DESCRIPTION_OLD in event.description:
        event.description = event.description.replace(DESCRIPTION_OLD, DESCRIPTION_NEW)
        event.save(update_fields=["description"])
    for item in PlanningItem.objects.filter(event=event, category="orga"):
        if item.title in KEPT_NOTES:
            item.notes = KEPT_NOTES[item.title]
        elif item.notes.startswith(DISCORD_PREFIX):
            item.notes = ""
        else:
            # Nachträglich von der Orga angelegte oder geänderte Einträge
            # bleiben unverändert.
            continue
        item.save(update_fields=["notes"])
    seed_items = PotluckItem.objects.filter(event=event, comment__startswith=BROUGHT_PREFIX)
    seed_items.filter(title__in=DISCARDED_POTLUCK_TITLES).delete()
    seed_items.update(comment="")


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0027_planningitem_last_done_by"),
    ]

    operations = [
        migrations.RunPython(merge_cake_into_bring, migrations.RunPython.noop),
        migrations.RunPython(cleanup_taunus_data, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="potluckitem",
            name="category",
            field=models.CharField(choices=[("bring", "Mitbringliste")], db_default="bring", default="bring", max_length=10, verbose_name="Liste"),
        ),
    ]
