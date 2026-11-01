from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Event, EventDay, GameEntry, OrgaActionLog, PlanningItem


class PlanningAuditTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="audit-orga")
        self.member = get_user_model().objects.create_user(username="audit-member")
        self.event = Event.objects.create(title="Audit", created_by=self.owner, planning_enabled=True)
        self.client.force_login(self.owner)
        self.item = PlanningItem.objects.create(event=self.event, title="Plakate", category="advert", people="Anna")

    def url(self, action, item=None):
        return reverse("events:planning_item_" + action, args=[self.event.pk, (item or self.item).pk])

    def payload(self, **extra):
        return {"title": "Plakate", "status": "open", "people": "Anna", "revision": self.item.revision, **extra}

    def test_two_editors_cannot_overwrite_and_inputs_are_preserved(self):
        first = self.client.get(self.url("edit")).context["form"]["revision"].value()
        second = self.client.get(self.url("edit")).context["form"]["revision"].value()
        self.assertEqual(self.client.post(self.url("edit"), self.payload(revision=first, people="Berta")).status_code, 302)
        stale = self.client.post(self.url("edit"), self.payload(revision=second, notes="Meine neuen Notizen"))
        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, "Meine neuen Notizen", status_code=409)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "Berta")
        self.assertEqual(self.item.notes, "")
        self.assertEqual(self.item.revision, 1)
        self.assertTrue(OrgaActionLog.objects.filter(description__contains="Anna → Berta").exists())
        self.assertEqual(self.client.post(self.url("edit"), {"title": "Ohne Version", "status": "open"}).status_code, 409)

    def test_delete_requires_current_revision_and_is_logged(self):
        self.client.post(self.url("edit"), self.payload(status="done"))
        self.client.post(self.url("delete"), {"revision": 0})
        self.assertTrue(PlanningItem.objects.filter(pk=self.item.pk).exists())
        self.client.post(self.url("delete"), {"revision": 1})
        self.assertFalse(PlanningItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(OrgaActionLog.objects.filter(description__contains="gelöscht").exists())

    def test_completion_json_conflict_and_no_js_fallback(self):
        todo = PlanningItem.objects.create(event=self.event, title="Mail", category="members")
        response = self.client.post(self.url("complete", todo), {"revision": 0, "done": "on"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.json(), {"status": "done", "label": "Erledigt", "revision": 1})
        stale = self.client.post(self.url("complete", todo), {"revision": 0}, HTTP_ACCEPT="application/json")
        self.assertEqual(stale.status_code, 409)
        todo.refresh_from_db()
        self.assertEqual(todo.status, "done")
        response = self.client.post(self.url("complete", todo), {"revision": 1})
        self.assertEqual(response.url, reverse("events:event_detail", args=[self.event.pk]) + f"#planning-{todo.pk}")
        todo.refresh_from_db()
        self.assertEqual(todo.status, "open")
        self.assertEqual(self.event.orga_action_logs.count(), 2)

    def test_advert_dates_links_validation_and_rendering(self):
        response = self.client.post(self.url("edit"), self.payload(due_date="2026-01-10", published_on="2026-01-11", reference_url="https://example.org/post"))
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.due_date, date(2026, 1, 10))
        response = self.client.get(reverse("events:event_detail", args=[self.event.pk]))
        self.assertContains(response, "10.01.2026")
        self.assertContains(response, "https://example.org/post")
        self.assertContains(response, "überfällig")
        invalid = self.client.post(self.url("edit"), self.payload(reference_url="javascript:alert(1)"))
        self.assertIn("reference_url", invalid.context["form"].errors)
        invalid = self.client.post(self.url("edit"), self.payload(reference_url="kein-link"))
        self.assertContains(invalid, 'class="form-more-options" open')

    def test_open_items_first_and_numbers_are_machine_readable(self):
        self.item.status = "done"
        self.item.save()
        unfinished = PlanningItem.objects.create(event=self.event, title="Flyer", category="advert")
        GameEntry.objects.create(event=self.event, name="Zahlen", bgg_rating=Decimal("7.95"), complexity=Decimal("3.75"))
        response = self.client.get(reverse("events:event_detail", args=[self.event.pk]))
        html = response.content.decode()
        self.assertLess(html.index(f'id="planning-{unfinished.pk}"'), html.index(f'id="planning-{self.item.pk}"'))
        self.assertIn('data-rating="7.95"', html)
        self.assertIn('data-complexity="3.75"', html)

    def test_new_endpoints_reject_non_managers_foreign_events_and_csrf(self):
        self.client.force_login(self.member)
        for name in ("planning_history", "planning_import"):
            self.assertEqual(self.client.get(reverse("events:" + name, args=[self.event.pk])).status_code, 403)
        for action in ("edit", "delete"):
            self.assertEqual(self.client.post(self.url(action), self.payload()).status_code, 403)
        other = Event.objects.create(title="Fremd", created_by=self.owner)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse("events:planning_item_delete", args=[other.pk, self.item.pk]), {"revision": 0}).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.url("delete"), {"revision": 0}).status_code, 403)
        self.assertEqual(csrf.post(reverse("events:planning_import", args=[self.event.pk]), {}).status_code, 403)

    def test_history_is_escaped_and_paged(self):
        OrgaActionLog.objects.bulk_create([OrgaActionLog(event=self.event, actor=self.owner, description="<script>test</script>") for _ in range(51)])
        response = self.client.get(reverse("events:planning_history", args=[self.event.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>test</script>")
        self.assertEqual(len(response.context["page"]), 50)

    def test_import_resets_shifts_and_is_idempotent(self):
        source = Event.objects.create(title="Vorjahr", created_by=self.owner, planning_enabled=True)
        EventDay.objects.create(event=source, date=date(2025, 11, 1))
        EventDay.objects.create(event=self.event, date=date(2026, 11, 1))
        original = PlanningItem.objects.create(event=source, category="advert", title="Zeitung", people="Mila", status="done", due_date=date(2025, 10, 10), published_on=date(2025, 10, 11), publication_url="https://example.org/old", reference_url="https://example.org", notes="Anzeigenformat")
        PlanningItem.objects.create(event=source, category="members", title="Reminder", todo_period="2_3")
        PlanningItem.objects.create(event=source, category="orga", title="Raum")
        url = reverse("events:planning_import", args=[self.event.pk])
        preview = self.client.get(url, {"source": source.pk})
        self.assertContains(preview, "Zeitung")
        self.assertEqual(self.event.planning_items.count(), 1)
        data = {"source": source.pk, "reset_status": "on", "reset_people": "on"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        item = self.event.planning_items.get(template_source=original)
        self.assertEqual((item.status, item.people, item.last_done_by), ("open", "", "Mila"))
        self.assertEqual(item.due_date, date(2026, 10, 10))
        self.assertIsNone(item.published_on)
        self.assertEqual(item.publication_url, "")
        self.assertEqual(item.reference_url, "https://example.org")
        self.assertEqual(self.event.planning_items.get(title="Reminder").todo_period, "2_3")
        self.client.post(url, data)
        self.assertEqual(self.event.planning_items.count(), 4)
        self.assertEqual(self.event.orga_action_logs.count(), 3)
        original.refresh_from_db()
        self.assertEqual(original.status, "done")

    def test_import_preserves_optional_assignments_and_clears_unknown_dates(self):
        source = Event.objects.create(title="Ohne Termin", created_by=self.owner, planning_enabled=True)
        PlanningItem.objects.create(event=source, title="Raum", people="Mila", status="progress", due_date=date(2025, 10, 10))
        url = reverse("events:planning_import", args=[self.event.pk])
        self.client.post(url, {"source": source.pk})
        item = self.event.planning_items.get(title="Raum")
        self.assertEqual((item.people, item.status), ("Mila", "progress"))
        self.assertIsNone(item.due_date)
        foreign = Event.objects.create(title="Geheim", created_by=self.member, planning_enabled=True)
        self.assertEqual(self.client.get(url, {"source": foreign.pk}).status_code, 404)
        self.assertEqual(self.client.post(url, {"source": foreign.pk}).status_code, 404)
        self.assertEqual(self.client.post(url, {"source": self.event.pk}).status_code, 404)

    def test_browser_fixture(self):
        from .models import GameBringClaim, GameExplainClaim
        for name, rating in (("Alpha", "7.95"), ("Beta", "7.10"), ("Gamma", "7.75"), ("Ohne Wertung", None)):
            game = GameEntry.objects.create(event=self.event, name=name, bgg_rating=Decimal(rating) if rating else None, complexity=Decimal(rating) / 2 if rating else None, created_by=self.owner)
            if name == "Alpha":
                GameBringClaim.objects.create(game=game, user=self.owner)
                GameExplainClaim.objects.create(game=game, user=self.owner)
        PlanningItem.objects.create(event=self.event, title="Raum buchen", people="Mila")
        PlanningItem.objects.create(event=self.event, title="Abgeschlossen", status="done")
        PlanningItem.objects.create(event=self.event, title="Erinnerung", category="members", todo_period="short")
        response = self.client.get(reverse("events:event_detail", args=[self.event.pk]))
        self.assertContains(response, "Meine Zusagen")
        self.assertContains(response, 'data-mine="true"')
        import os
        from pathlib import Path
        if os.environ.get("AUDIT_HTML_PATH"):
            Path(os.environ["AUDIT_HTML_PATH"]).write_bytes(response.content)

    def test_admin_edits_invalidate_open_forms_and_reject_stale_revision(self):
        self.owner.is_staff = True
        self.owner.is_superuser = True
        self.owner.save()
        url = reverse("admin:events_planningitem_change", args=[self.item.pk])
        payload = self.payload(event=self.event.pk, category="advert", people="Admin", edit_revision=0, _save="Speichern")
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "Admin")
        self.assertEqual(self.item.revision, 1)
        self.assertEqual(self.client.post(self.url("edit"), self.payload(revision=0)).status_code, 409)
        response = self.client.post(url, payload)
        self.assertContains(response, "inzwischen geändert")
        self.item.refresh_from_db()
        self.assertEqual(self.item.revision, 1)
