from django.core.management.base import BaseCommand

from events.notifications import send_pending_idea_comment_notifications


class Command(BaseCommand):
    help = (
        "Sendet gesammelte E-Mail-Benachrichtigungen für Ideen-Kommentare, sofern "
        "die älteste offene Benachrichtigung eines Empfängers mindestens eine "
        "Stunde alt ist. Gedacht zum periodischen Aufruf per Cron."
    )

    def handle(self, *args, **options):
        sent_count = send_pending_idea_comment_notifications()
        self.stdout.write(self.style.SUCCESS(f"{sent_count} Benachrichtigungs-Mail(s) verschickt."))
