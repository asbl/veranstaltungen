import datetime
import json
import logging
import sys
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .app_log import JsonLineFormatter, send_error_report
from .models import Duty, DutySignup, Event


class RequestAuditMiddlewareTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="orga")
        self.member = get_user_model().objects.create_user(username="schotti")
        self.event = Event.objects.create(title="Spieltag", planning_enabled=True, created_by=self.owner)
        self.duty = Duty.objects.create(event=self.event, title="Essen und Getränke", needed_people=1)
        self.client.force_login(self.member)

    def entries(self, logs):
        return [record.audit for record in logs.records]

    def test_successful_signup_is_logged_with_user_and_status(self):
        with self.assertLogs("events.audit", level="INFO") as logs:
            self.client.post(reverse("events:duty_signup", args=[self.duty.pk]))
        [entry] = self.entries(logs)
        self.assertEqual(entry["user"], "schotti")
        self.assertEqual(entry["method"], "POST")
        self.assertEqual(entry["path"], reverse("events:duty_signup", args=[self.duty.pk]))
        self.assertEqual(entry["status"], 302)
        self.assertEqual(logs.records[0].levelno, logging.INFO)
        self.assertTrue(DutySignup.objects.filter(duty=self.duty, user=self.member).exists())

    def test_error_message_shown_to_user_is_logged_as_warning(self):
        DutySignup.objects.create(duty=self.duty, user=self.owner)
        with self.assertLogs("events.audit", level="INFO") as logs:
            self.client.post(reverse("events:duty_signup", args=[self.duty.pk]))
        [entry] = self.entries(logs)
        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        self.assertIn("keine weiteren Personen", entry["user_messages"][0])

    def test_csrf_failure_is_logged_as_warning(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        with self.assertLogs("events.audit", level="INFO") as logs:
            client.post(reverse("events:duty_signup", args=[self.duty.pk]))
        [entry] = self.entries(logs)
        self.assertEqual(entry["status"], 403)
        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        self.assertFalse(DutySignup.objects.filter(duty=self.duty, user=self.member).exists())

    def test_plain_page_views_and_anonymous_404_are_not_logged(self):
        self.client.logout()
        with self.assertNoLogs("events.audit", level="INFO"):
            self.client.get("/gibt-es-nicht/")

    def test_query_string_is_not_logged(self):
        with self.assertLogs("events.audit", level="INFO") as logs:
            self.client.post(reverse("events:duty_signup", args=[self.duty.pk]) + "?code=geheim")
        self.assertNotIn("geheim", json.dumps(self.entries(logs)))


class ErrorReportTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "app.jsonl"
        self.override = override_settings(APP_LOG_FILE=str(self.path), ERROR_REPORT_EMAILS=["admin@example.org"])
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        self.directory.cleanup()

    def write(self, *entries, day="2026-10-30"):
        with self.path.open("a", encoding="utf-8") as handle:
            for level, message in entries:
                handle.write(json.dumps({"time": f"{day}T08:00:00+01:00", "level": level, "message": message, "user": "schotti", "method": "POST", "path": "/x/"}) + "\n")

    def test_only_new_warnings_and_errors_are_mailed_once(self):
        self.write(("INFO", "ok"), ("WARNING", "Anfrage abgelehnt: 403"), ("ERROR", "Serverfehler: 500"))
        today = datetime.date(2026, 10, 30)
        self.assertEqual(send_error_report(today), 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Anfrage abgelehnt: 403", mail.outbox[0].body)
        self.assertIn("schotti", mail.outbox[0].body)
        self.assertNotIn("  ok", mail.outbox[0].body)
        self.assertEqual(send_error_report(today), 0)
        self.write(("ERROR", "neu"))
        self.assertEqual(send_error_report(today), 1)
        self.assertEqual(len(mail.outbox), 2)

    def test_mail_failure_keeps_entries_for_next_run(self):
        self.write(("ERROR", "kaputt"))
        today = datetime.date(2026, 10, 30)
        with override_settings(EMAIL_BACKEND="events.test_app_log.FailingBackend"):
            with self.assertRaises(OSError):
                send_error_report(today)
        self.assertEqual(send_error_report(today), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_rotates_daily_and_prunes_after_retention(self):
        old = Path(self.directory.name) / "app-2026-07-01.jsonl"
        old.write_text("")
        self.write(("ERROR", "gestern"), day="2026-10-29")
        self.assertEqual(send_error_report(datetime.date(2026, 10, 30)), 1)
        self.assertFalse(self.path.exists())
        self.assertTrue((Path(self.directory.name) / "app-2026-10-29.jsonl").exists())
        self.assertFalse(old.exists())
        self.write(("ERROR", "heute"))
        self.assertEqual(send_error_report(datetime.date(2026, 10, 30)), 1)
        self.assertIn("heute", mail.outbox[-1].body)

    def test_formatter_writes_traceback_and_request_user(self):
        try:
            raise ValueError("Testfehler")
        except ValueError:
            record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Internal Server Error", (), sys.exc_info())
        entry = json.loads(JsonLineFormatter().format(record))
        self.assertEqual(entry["level"], "ERROR")
        self.assertIn("ValueError: Testfehler", entry["traceback"])


class FailingBackend:
    def __init__(self, *args, **kwargs):
        pass

    def send_messages(self, messages):
        raise OSError("SMTP nicht erreichbar")
