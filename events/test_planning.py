from datetime import time
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .forms import DutyScheduleForm
from .models import Duty, DutySignup, Event, EventDay, PlanningItem, PotluckItem
from .planning import create_duty_grid, create_duty_schedule


class PlanningTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="orga")
        self.member = get_user_model().objects.create_user(username="member")
        self.event = Event.objects.create(title="Öffentlicher Spieltag", planning_enabled=True, created_by=self.owner)
        self.client.force_login(self.owner)

    def test_schedule_clips_last_slot_and_allows_multiple_people(self):
        duties = create_duty_schedule(event=self.event, title="Kasse", start_time=time(10), end_time=time(18), slot_hours=3, needed_people=2)
        self.assertEqual([(d.start_time, d.end_time) for d in duties], [(time(10), time(13)), (time(13), time(16)), (time(16), time(18))])
        for user in (self.owner, self.member):
            self.client.force_login(user)
            self.client.post(reverse("events:duty_signup", args=[duties[0].pk]))
        self.assertTrue(duties[0].is_full)

    def test_reserve_does_not_fill_regular_spot_and_can_join_full_duty(self):
        duty = Duty.objects.create(event=self.event, title="Kasse", needed_people=1)
        url = reverse("events:duty_signup", args=[duty.pk])
        self.client.post(url, {"role": "reserve"})
        self.assertEqual(duty.signup_count, 0)
        self.assertFalse(duty.is_full)
        self.client.force_login(self.member)
        self.client.post(url)
        self.assertTrue(duty.is_full)
        third = get_user_model().objects.create_user(username="third")
        self.client.force_login(third)
        self.client.post(url)
        self.assertEqual(duty.signups.count(), 2)
        self.client.post(url, {"role": "reserve"})
        self.client.post(url, {"role": "reserve"})
        self.assertEqual(duty.signups.count(), 3)
        self.assertEqual(duty.signup_count, 1)

    def test_fractional_slots_and_invalid_ranges(self):
        duties = create_duty_schedule(event=self.event, title="Test", start_time=time(10), end_time=time(11, 15), slot_hours=Decimal("0.5"))
        self.assertEqual(len(duties), 3)
        self.assertEqual(duties[-1].end_time, time(11, 15))
        for hours in (0, -1, 25):
            with self.assertRaises(ValidationError):
                create_duty_schedule(event=self.event, title="Test", start_time=time(10), end_time=time(18), slot_hours=hours)
        with self.assertRaises(ValidationError):
            create_duty_schedule(event=self.event, title="Test", start_time=time(18), end_time=time(10), slot_hours=3)

    def test_schedule_form_rejects_foreign_day_missing_times_and_zero(self):
        other = Event.objects.create(title="Andere Veranstaltung")
        day = EventDay.objects.create(event=other, date="2026-01-01")
        base = {"titles": "Kasse", "start_time": "10:00", "end_time": "18:00", "slot_hours": "2"}
        for changes in ({"day": day.pk}, {"end_time": ""}, {"slot_hours": "0"}, {"titles": "  \n"}, {"start_time": "18:00"}):
            self.assertFalse(DutyScheduleForm({**base, **changes}, event=self.event).is_valid())
        with self.assertRaises(ValidationError):
            create_duty_schedule(event=self.event, day=day, title="Test", start_time=time(10), end_time=time(18), slot_hours=3)

    def test_duty_grid_creates_every_duty_per_slot_with_springer(self):
        duties = create_duty_grid(
            event=self.event, titles=["Kasse", "Info"], start_time=time(10), end_time=time(14), slot_hours=2,
        )
        self.assertEqual(len(duties), 6)
        kasse = [(d.start_time, d.end_time) for d in self.event.duties.filter(title="Kasse").order_by("start_time")]
        self.assertEqual(kasse, [(time(10), time(12)), (time(12), time(14))])
        springer = [(d.start_time, d.end_time) for d in self.event.duties.filter(title="Springer").order_by("start_time")]
        self.assertEqual(springer, [(time(10), time(12)), (time(12), time(14))])
        with self.assertRaises(ValidationError):
            create_duty_grid(event=self.event, titles=["  "], start_time=time(10), end_time=time(12), slot_hours=2)

    def test_schedule_view_permissions_and_creation(self):
        url = reverse("events:duty_schedule_create", args=[self.event.pk])
        data = {"titles": "Kasse\nInfo", "start_time": "10:00", "end_time": "16:00", "slot_hours": "2", "needed_people": 2}
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertFalse(self.event.duties.exists())
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        # 2 Dienste × 3 Zeiträume, ohne Springer (Häkchen nicht gesetzt)
        self.assertEqual(self.event.duties.count(), 6)
        self.assertEqual(self.event.duties.filter(title="Springer").count(), 0)
        self.client.post(url, {**data, "titles": "Kasse", "create_reserve": "on"})
        self.assertEqual(self.event.duties.filter(title="Kasse").count(), 6)
        self.assertEqual(self.event.duties.filter(title="Springer").count(), 3)

    def test_planning_crud_is_orga_only_and_scoped_to_event(self):
        url = reverse("events:planning_item_create", args=[self.event.pk])
        data = {"category": "orga", "title": "Schilder", "status": "waiting", "people": "Sabine / Nadine", "last_done_by": "Mila"}
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        item = self.event.planning_items.get()
        self.assertEqual(item.people, "Sabine / Nadine")
        self.assertEqual(item.last_done_by, "Mila")
        edit = reverse("events:planning_item_edit", args=[self.event.pk, item.pk])
        self.assertEqual(self.client.post(edit, {**data, "status": "done", "revision": item.revision}).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.status, "done")
        other = Event.objects.create(title="Other", created_by=self.owner)
        self.assertEqual(self.client.post(reverse("events:planning_item_edit", args=[other.pk, item.pk]), data).status_code, 404)
        delete = reverse("events:planning_item_delete", args=[self.event.pk, item.pk])
        self.assertEqual(self.client.get(delete).status_code, 405)
        self.assertEqual(self.client.post(delete, {"revision": item.revision}).status_code, 302)
        self.assertFalse(PlanningItem.objects.exists())

    def test_event_access_gate_applies_to_new_endpoints(self):
        self.event.set_access_password("secret")
        self.event.save()
        self.client.force_login(self.member)
        for name in ("planning_item_create", "duty_schedule_create"):
            self.assertEqual(self.client.post(reverse(f"events:{name}", args=[self.event.pk])).status_code, 403)

    def test_lists_render_together_and_escape_input(self):
        PotluckItem.objects.create(event=self.event, title="Schokokuchen")
        PotluckItem.objects.create(event=self.event, title="Kaffeekanne")
        PlanningItem.objects.create(event=self.event, title="<script>alert(1)</script>", people="Sabine")
        PlanningItem.objects.create(event=self.event, title="Schilder erstellen", last_done_by="Sabine / Nadine")
        response = self.client.get(reverse("events:event_detail", args=[self.event.pk]))
        self.assertEqual(response.status_code, 200)
        # Die Kuchenliste ist in der Mitbringliste aufgegangen.
        self.assertContains(response, "Mitbringliste (2)")
        self.assertNotContains(response, "Kuchenliste")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "Werbung")
        # Offene Aufgaben: roter Status, roter „Noch offen“-Badge in der
        # Personen-Spalte und der Vorjahres-Hinweis außerhalb des Badges;
        # sobald Personen eingetragen sind, entfällt der Hinweis.
        self.assertContains(response, 'class="pill danger">Offen</span>')
        self.assertContains(response, 'class="pill danger">Noch offen</span>')
        self.assertContains(response, "zuletzt gemacht von Sabine / Nadine")
        self.assertEqual(response.content.decode().count("zuletzt gemacht von"), 1)
        self.assertEqual([i.title for i in response.context["potluck_items"]], ["Kaffeekanne", "Schokokuchen"])

    def test_preparation_is_repeatable_and_keeps_edits(self):
        call_command("prepare_taunus26", access_code="test-schluessel", stdout=StringIO())
        event = Event.objects.get(title="Taunus spielt 26")
        self.assertTrue(event.check_access_password("test-schluessel"))
        self.assertFalse(event.days.exists())
        # 5 Dienste × 4 Zeiträume + 4 Springer + Aufbau + Abbau
        self.assertEqual(event.duties.count(), 26)
        self.assertEqual(event.duties.filter(title="Springer").count(), 4)
        self.assertFalse(event.duties.exclude(description="").exists())
        kasse = [(d.start_time, d.end_time) for d in event.duties.filter(title="Eintritt / Kasse").order_by("start_time")]
        self.assertEqual(kasse, [(time(10), time(12)), (time(12), time(14)), (time(14), time(16)), (time(16), time(18))])
        self.assertEqual(event.planning_items.filter(category="orga").count(), 13)
        self.assertFalse(event.planning_items.exclude(category="members").exclude(people="").exists())
        self.assertEqual(event.planning_items.filter(category="members", people="Orga").count(), 3)
        self.assertFalse(event.planning_items.exclude(status="open").exists())
        self.assertEqual(event.planning_items.get(title="Schilder erstellen").last_done_by, "Orga")
        self.assertEqual(event.planning_items.get(category="advert", title="Presse / Lokalzeitung").last_done_by, "Orga")
        # Notizen nur mit für 2026 wieder relevanten Informationen; kein
        # damaliger Discord-Stand.
        self.assertNotIn("Discord", event.description)
        self.assertEqual(event.planning_items.get(title="Schilder erstellen").notes, "")
        self.assertEqual(
            event.planning_items.get(title="Kassen für Flohmarkt, Essen und Eintritt organisieren").notes,
            "Kassen wurden vom Orga-Team mitgebracht (bitte prüfen).",
        )
        item = event.planning_items.first()
        item.status = "done"
        item.save()
        call_command("prepare_taunus26", access_code="test-schluessel", stdout=StringIO())
        item.refresh_from_db()
        self.assertEqual(item.status, "done")
        self.assertEqual(event.duties.count(), 26)
        self.assertEqual(event.potluck_items.count(), 1)
        self.assertEqual(event.potluck_items.get(title="Kaffee").comment, "")
        call_command("prepare_taunus26", date="2026-11-01", stdout=StringIO())
        self.assertEqual(event.days.count(), 1)
        self.assertFalse(event.duties.filter(day__isnull=True).exists())
        self.assertEqual(event.days.get().start_time, time(10))
        self.assertEqual(event.duties.get(category="setup").start_time, time(9))
        self.assertEqual(event.duties.get(category="setup").end_time, time(10))
        self.assertEqual(event.duties.get(category="teardown").start_time, time(18))
        self.assertEqual(event.duties.get(category="teardown").end_time, time(19))

    def test_planning_potluck_counts_volunteers_without_attendance(self):
        from .models import PotluckClaim
        item = PotluckItem.objects.create(event=self.event, title="Kuchen", needed_quantity=2)
        PotluckClaim.objects.create(item=item, user=self.member, quantity=2)
        self.assertEqual(item.claimed_quantity, 2)
        self.assertEqual(item.claimed_person_count, 1)
        self.assertFalse(item.is_missing)
        response = self.client.get(reverse("events:event_detail", args=[self.event.pk]))
        self.assertEqual(response.context["potluck_items"][0].claimed_quantity, 2)
        self.assertTrue(response.context["potluck_items"][0].claims.all()[0].user_is_attending)

    def test_planning_event_can_be_edited_without_a_date_then_assigns_pending_duties(self):
        duty = Duty.objects.create(event=self.event, title="Aufbau", start_time=time(9))
        url = reverse("events:event_edit", args=[self.event.pk])
        data = {"title": self.event.title, "planning_enabled": "on", "days-TOTAL_FORMS": "1", "days-INITIAL_FORMS": "0", "days-MIN_NUM_FORMS": "0", "days-MAX_NUM_FORMS": "1000"}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.event.days.exists())
        response = self.client.post(url, {**data, "days-0-date": "2027-11-07", "days-0-start_time": "10:00", "days-0-end_time": "18:00"})
        self.assertEqual(response.status_code, 302)
        duty.refresh_from_db()
        self.assertEqual(duty.day_id, self.event.days.get().pk)

    def test_reserve_signup_is_limited_to_service_duties(self):
        setup = Duty.objects.create(event=self.event, title="Aufbau", category="setup")
        self.client.post(reverse("events:duty_signup", args=[setup.pk]), {"role": "reserve"})
        signup = setup.signups.get(user=self.owner)
        self.assertFalse(signup.is_reserve)
        self.assertEqual(setup.signup_groups, [("Besetzung", [signup])])

    def test_invalid_seed_date_does_not_create_an_event(self):
        from django.core.management.base import CommandError
        for value in ("bad", "2027-11-01"):
            with self.assertRaises(CommandError):
                call_command("prepare_taunus26", date=value, stdout=StringIO())
        self.assertFalse(Event.objects.filter(title="Taunus spielt 26").exists())

    def test_reserve_can_cancel_but_cannot_cancel_other_people(self):
        duty = Duty.objects.create(event=self.event, title="Test")
        signup = DutySignup.objects.create(duty=duty, user=self.member, is_reserve=True)
        url = reverse("events:duty_signup_cancel", args=[signup.pk])
        outsider = get_user_model().objects.create_user(username="outsider")
        self.client.force_login(outsider)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(duty.signups.exists())

    def test_advert_edit_preserves_category_and_empty_posts_show_errors(self):
        item = PlanningItem.objects.create(event=self.event, category="advert", title="VHS")
        url = reverse("events:planning_item_edit", args=[self.event.pk, item.pk])
        response = self.client.get(url)
        self.assertEqual(response.context["form"]["category"].value(), "advert")
        response = self.client.post(url, {})
        self.assertTrue(response.context["form"].is_bound)
        self.assertIn("title", response.context["form"].errors)

    def test_planning_forms_match_their_list_and_keep_category_on_edit(self):
        url = reverse("events:planning_item_create", args=[self.event.pk])
        for category, title in (("orga", "Verantwortlichkeit"), ("advert", "Wo wird Werbung gemacht?"), ("members", "Aufgabe / Anfrage / Reminder")):
            with self.subTest(category=category):
                response = self.client.get(url, {"category": category})
                form = response.context["form"]
                self.assertEqual(form.fields["title"].label, title)
                self.assertTrue(form.fields["category"].widget.is_hidden)
                self.assertEqual("todo_period" in form.fields, category == "members")
                self.assertNotContains(response, '<select name="category"')
                response = self.client.post(url + f"?category={category}", {"title": "Test", "status": "open"})
                self.assertEqual(response.status_code, 302)
                item = self.event.planning_items.latest("pk")
                self.assertEqual(item.category, category)
                edit = reverse("events:planning_item_edit", args=[self.event.pk, item.pk])
                response = self.client.post(edit, {"category": "members" if category != "members" else "advert", "title": "Geändert", "status": "done", "revision": item.revision})
                self.assertEqual(response.status_code, 302)
                item.refresh_from_db()
                self.assertEqual(item.category, category)
                response = self.client.post(edit, {})
                self.assertEqual(response.context["form"].fields["title"].label, title)
                self.assertContains(response, f'{response.context["form"].presentation["name"]} bearbeiten', status_code=409)


    def test_member_todo_creation_completion_and_permissions(self):
        url = reverse("events:planning_item_create", args=[self.event.pk])
        response = self.client.get(url + "?category=members")
        self.assertEqual(response.context["form"]["category"].value(), "members")
        data = {"category": "members", "title": "Reminder Kuchen", "people": "Mila", "status": "open", "todo_period": "2_3"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        item = self.event.planning_items.get()
        self.assertEqual(item.todo_period, "2_3")
        self.assertEqual(item.people, "Mila")
        self.assertContains(self.client.get(reverse("events:event_detail", args=[self.event.pk])), "2–3 Wochen vorher")
        complete = reverse("events:planning_item_complete", args=[self.event.pk, item.pk])
        self.assertEqual(self.client.get(complete).status_code, 405)
        self.assertEqual(self.client.post(complete, {"done": "on", "revision": item.revision}).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.status, "done")
        detail = reverse("events:event_detail", args=[self.event.pk])
        response = self.client.get(detail)
        self.assertContains(response, 'class="todo-completed"')
        self.assertContains(response, 'aria-label="Erledigt: Reminder Kuchen" checked')
        self.client.force_login(self.member)
        response = self.client.get(detail)
        self.assertContains(response, 'class="todo-completed"')
        self.assertContains(response, 'disabled aria-label="Erledigt: Reminder Kuchen" checked')
        self.client.force_login(self.owner)
        self.client.post(complete, {"revision": item.revision})
        item.refresh_from_db()
        self.assertEqual(item.status, "open")
        self.assertNotContains(self.client.get(detail), 'class="todo-completed"')
        other = Event.objects.create(title="Andere", created_by=self.owner)
        self.assertEqual(self.client.post(reverse("events:planning_item_complete", args=[other.pk, item.pk]), {"done": "on"}).status_code, 404)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(complete, {"done": "on", "revision": item.revision}).status_code, 403)
        self.client.force_login(self.owner)
        response = self.client.post(url, {**data, "todo_period": "invalid"})
        self.assertIn("todo_period", response.context["form"].errors)

    def test_member_request_migration_keeps_existing_work(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        item = PlanningItem.objects.create(event=self.event, title="Kuchenanfrage an die Mitglieder", people="Mila", status="done", notes="Beibehalten")
        migrate = import_module("events.migrations.0032_member_request_todos").move_requests
        with connection.schema_editor(atomic=False) as editor:
            migrate(apps, editor)
            migrate(apps, editor)
        item.refresh_from_db()
        self.assertEqual((item.category, item.people, item.status, item.notes), ("members", "Mila", "done", "Beibehalten"))
        self.assertEqual(self.event.planning_items.filter(category="members").count(), 2)
        self.assertFalse(self.event.planning_items.filter(category="orga").exists())


    def test_todos_follow_orga_before_duties_and_show_all_periods(self):
        PlanningItem.objects.create(event=self.event, category="members", title="Raum buchen", todo_period="year", people="Testperson")
        response = self.client.get(reverse("events:event_detail", args=[self.event.pk]))
        html = response.content.decode()
        self.assertLess(html.index('id="orga-section"'), html.index('id="members-section"'))
        self.assertLess(html.index('id="members-section"'), html.index('id="duties-section"'))
        self.assertNotContains(response, "Dienste &amp; Orga")
        self.assertEqual(html.count('id="members-section"'), 1)
        for key, label in PlanningItem.TODO_PERIODS:
            self.assertContains(response, f"<h3>{label}</h3>")
        self.assertEqual(response.context["todo_groups"][0][2][0].title, "Raum buchen")
        url = reverse("events:planning_item_create", args=[self.event.pk])
        response = self.client.get(url + "?category=members&period=year")
        self.assertEqual(response.context["form"]["todo_period"].value(), "year")

    def test_todo_group_migration_preserves_tasks_and_adds_default_tasks_once(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        self.event.title = "Taunus spielt 26"
        self.event.save()
        item = PlanningItem.objects.create(event=self.event, category="members", title="Anfrage", weeks_before=7, people="Mila", status="done")
        migrate = import_module("events.migrations.0034_group_todos").group_todos
        with connection.schema_editor(atomic=False) as editor:
            migrate(apps, editor)
            migrate(apps, editor)
        item.refresh_from_db()
        self.assertEqual((item.todo_period, item.people, item.status), ("6_8", "Mila", "done"))
        self.assertEqual(self.event.planning_items.filter(people="Orga").count(), 3)
