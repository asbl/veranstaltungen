from django.core.management.base import BaseCommand

from events.app_log import send_error_report


class Command(BaseCommand):
    help = (
        "Verschickt neue Warnungen und Fehler aus logs/app.jsonl gesammelt per Mail "
        "an ERROR_REPORT_EMAILS, rotiert das Protokoll täglich und löscht alte "
        "Tagesdateien. Gedacht zum periodischen Aufruf per Cron."
    )

    def handle(self, *args, **options):
        count = send_error_report()
        self.stdout.write(self.style.SUCCESS(f"{count} Fehler/Warnung(en) gemeldet."))
