import os
from datetime import date, time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from events.models import Duty, Event, EventDay, PlanningItem, PotluckItem
from events.planning import create_duty_grid

SHEET = "https://docs.google.com/spreadsheets/d/18N9bUcY-ekpifgJLiyNtZMJQSHqqqLkhD9PKbt1xo-U/edit?usp=sharing"
VHS = "https://www.vhs-badhomburg.de/p/kurskompass-website/politik-gesellschaft-umwelt/taunus-spielt-der-brettspieltag-in-usingen-474-C-262D18-55"
# Die Vorjahrespersonen stehen als Orientierung in "Wer hat es das letzte Mal
# gemacht" (last_done_by); die Person(en)-Spalte startet leer. Für die nächste
# Ausgabe der Veranstaltung kann das Feld als Vorlage übernommen werden.
# Notizen nur mit Informationen, die für 2026 wieder relevant sind; der
# damalige Bearbeitungsstand wird nicht archiviert.
TASKS = [
    ("Schilder erstellen", "Orga", "", ""),
    ("Preisliste erstellen", "", "", ""),
    ("Getränkemärkte: Apfelschorle und Wasser auf Kommission", "Orga", "", ""),
    ("30 Brezeln bestellen", "Orga", "", ""),
    ("Warme Mahlzeit (Grillen, Chili, ...) planen", "", "Feedback 2025: an längeren Spieletagen eine warme Mahlzeit einplanen, z. B. Chili, das weiterkocht und bei dem man sich immer wieder bedienen kann.", ""),
    ("Pappteller, Milch, Müllsäcke, Crêpe-Band, Reißzwecken, Stifte, ggf. Namensschilder beschaffen", "Orga", "", ""),
    ("T-Shirts: aktuellen Stand einholen", "Orga", "", ""),
    ("Presse einladen", "Orga", "Pressekontakt des Vorjahres für Samstag oder Sonntag einladen.", ""),
    ("Liste für Erklärbären erstellen", "Orga", "", ""),
    ("BoardGameCircus anschreiben", "Orga", "", ""),
    ("Pegasus anschreiben", "Orga", "Pegasus stellte Spiele.", ""),
    ("Kassen für Flohmarkt, Essen und Eintritt organisieren", "Orga", "Kassen wurden vom Orga-Team mitgebracht (bitte prüfen).", ""),
    ("Catan-Turnier organisieren", "", "Für 2026: Turnierleitung, Anmeldung, Regeln, Material, Startzeit und Teilnehmerzahl klären.", ""),
]
MEMBER_TASKS = [
    ("Kuchenanfrage an die Mitglieder", "Orga", "", SHEET),
    ("Dienstanfrage an die Mitglieder (2-Stunden-Schichten)", "Orga", "", SHEET),
    ("Dienstanfrage für Abbau", "Orga", "", SHEET),
    ("Reminder: Kuchenanfrage an die Mitglieder", "", "", ""),
    ("Reminder: Dienstanfrage an die Mitglieder", "", "", ""),
    ("Reminder: Dienstanfrage für Abbau", "", "", ""),
]
ADVERTS = [
    ("VHS-Veranstaltungskalender", "", VHS),
    ("Vereinswebsite", "", ""),
    ("Discord / Mitglieder", "", ""),
    ("Presse / Lokalzeitung", "Orga", ""),
    ("Plakate und Flyer", "", ""),
    ("Social Media", "", ""),
]
POTLUCK = ["Kaffee"]
SERVICE_DUTIES = ["Eintritt / Kasse", "Essen und Getränke", "Flohmarkt", "Erklärbären", "Catan-Turnier"]


class Command(BaseCommand):
    help = "Bereitet Taunus spielt 26 einmalig vor; erneute Aufrufe überschreiben keine Listen."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Bestätigtes Veranstaltungsdatum 2026 (YYYY-MM-DD)")
        parser.add_argument(
            "--access-code",
            help="Zugangsschlüssel der Veranstaltung (alternativ ENV TAUNUS26_ACCESS_CODE). "
                 "Ohne Angabe wird kein Passwort gesetzt - das Schlüsselwort wird nie im Code abgelegt.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        event_date = None
        if options.get("date"):
            try:
                event_date = date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("Datum muss YYYY-MM-DD sein.") from exc
            if event_date.year != 2026:
                raise CommandError("Für Taunus spielt 26 wird ein Datum im Jahr 2026 erwartet.")
        event, created = Event.objects.get_or_create(title="Taunus spielt 26", defaults={
            "planning_enabled": True,
            # Wer schonmal bei einer beendeten Veranstaltung dabei war, kommt
            # mit E-Mail-Code ohne Zugangspasswort hinein (siehe Migration 0026).
            "allow_known_persons_signup": True,
            "location": "Veranstaltungsort für 2026 noch bestätigen",
            "description": "Öffentlicher Brettspieltag 2026 mit Catan-Turnier.\n"
                "Veranstaltung: 10:00–18:00 Uhr. Aufbau 09:00–10:00 Uhr, Abbau 18:00–19:00 Uhr.\n"
                "Die Aufgaben sind für 2026 neu zu bestätigen.",
        })
        if not event.has_access_password:
            access_code = options.get("access_code") or os.environ.get("TAUNUS26_ACCESS_CODE", "")
            if access_code:
                event.set_access_password(access_code)
                event.save(update_fields=["access_password_hash"])
            else:
                self.stdout.write(self.style.WARNING(
                    "Kein Zugangsschlüssel gesetzt (--access-code oder TAUNUS26_ACCESS_CODE fehlt); "
                    "die Veranstaltung bleibt ohne Passwort zugänglich."
                ))
        if event_date:
            existing_day = event.days.first()
            if existing_day and existing_day.date != event_date:
                raise CommandError("Ein anderes Datum existiert bereits; bitte über die Veranstaltungsbearbeitung ändern.")
            day, _ = EventDay.objects.get_or_create(event=event, date=event_date, defaults={"start_time": time(10), "end_time": time(18)})
            event.duties.filter(day__isnull=True).update(day=day)
        else:
            day = event.days.first()
        if not created:
            self.stdout.write(f"Veranstaltung {event.pk} vorhanden; Listen unverändert.")
            return
        for title, last_done_by, notes, url in TASKS:
            PlanningItem.objects.create(event=event, title=title, last_done_by=last_done_by, notes=notes, reference_url=url)
        for title, last_done_by, notes, url in MEMBER_TASKS:
            PlanningItem.objects.create(event=event, category="members", todo_period="2_3" if title.startswith("Reminder:") else "4_5", title=title, last_done_by=last_done_by, notes=notes, reference_url=url)
        for title, period in [
            ("Veranstaltungsraum organisieren", "year"),
            ("Planungstreffen organisieren", "6_8"),
            ("Spiele-Erklärerliste drucken und mitnehmen", "short"),
        ]:
            PlanningItem.objects.create(event=event, category="members", title=title, todo_period=period, people="Orga")
        for title, last_done_by, url in ADVERTS:
            PlanningItem.objects.create(event=event, category="advert", title=title, last_done_by=last_done_by, reference_url=url)
        for title in POTLUCK:
            PotluckItem.objects.create(event=event, title=title)
        # Individual cakes are entered by their contributors; no invented promises.
        # Uhrzeitraster 10–18 Uhr in 2-Stunden-Zeiträumen, jeder Dienst einmal je
        # Zeitraum, zusätzlich ein Springer-Dienst je Zeitraum (Auf-/Abbau separat).
        create_duty_grid(event=event, titles=SERVICE_DUTIES, day=day, start_time=time(10), end_time=time(18), slot_hours=2)
        Duty.objects.create(event=event, title="Aufbau", category="setup", day=day, start_time=time(9), end_time=time(10))
        Duty.objects.create(event=event, title="Abbau", category="teardown", day=day, start_time=time(18), end_time=time(19))
        self.stdout.write(self.style.SUCCESS(f"Taunus spielt 26 vorbereitet: /{event.pk}/"))
