from django.conf import settings
from django.core.management.base import BaseCommand

from events.translations import refresh_changed_translations, translate_missing_games


class Command(BaseCommand):
    help = (
        "Übersetzt automatisch einen kleinen Batch von Spielen, für die noch keine "
        "gemeinsame deutsche BGG-Beschreibung existiert, und übersetzt automatisch "
        "übersetzte Beschreibungen neu, deren BGG-Original sich geändert hat (je "
        "eine DeepL-Anfrage je BGG-ID und Quellfassung). Manuell bearbeitete "
        "Übersetzungen werden nie angefasst; fehlgeschlagene Anfragen werden nur "
        "bei einer neuen Quellfassung erneut versucht. Gedacht zum periodischen "
        "Aufruf per Cron."
    )

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=5)

    def handle(self, *args, **options):
        if not settings.DEEPL_API_KEY:
            self.stdout.write("DeepL ist nicht konfiguriert - keine automatischen Übersetzungen.")
            return
        batches = [
            ("Neu übersetzt", translate_missing_games(limit=options["batch_size"])),
            ("Neue Quellfassung", refresh_changed_translations(limit=options["batch_size"])),
        ]
        if not any(rows for _, rows in batches):
            self.stdout.write("Keine ausstehenden Übersetzungen.")
            return
        new_ready = updated = failed = 0
        for label, rows in batches:
            for bgg_id, status, error in rows:
                if status != "ready":
                    failed += 1
                    reason = error or f"Status {status}."
                    self.stderr.write(f"{label}, BGG-ID {bgg_id}: {reason}")
                elif label == "Neu übersetzt":
                    new_ready += 1
                else:
                    updated += 1
        parts = []
        if new_ready:
            parts.append(f"{new_ready} Übersetzung(en) neu erstellt")
        if updated:
            parts.append(f"{updated} aktualisiert")
        if failed:
            parts.append(f"{failed} nicht erfolgreich")
        self.stdout.write(self.style.SUCCESS(", ".join(parts) + "."))
