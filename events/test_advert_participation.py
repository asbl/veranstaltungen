from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from .models import Event, PlanningItem
from .forms import PlanningItemForm


class AdvertParticipationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="owner")
        self.member = get_user_model().objects.create_user(username="member")
        self.event = Event.objects.create(title="Spieltag", planning_enabled=True, created_by=self.owner)
        self.item = PlanningItem.objects.create(event=self.event, category="advert", title="Im eigenen Signal-Status", everyone=True)
        self.url = reverse("events:planning_item_participate", args=[self.event.pk, self.item.pk])
        self.client.force_login(self.member)

    def test_participation_is_personal_idempotent_and_leaves_task_open(self):
        for user in (self.member, self.owner, self.member):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.url, {"user": self.owner.pk}).status_code, 302)
        self.assertEqual(self.item.completed_by.count(), 2)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "open")
        page = self.client.get(reverse("events:event_detail", args=[self.event.pk]))
        self.assertContains(page, "Schon gemacht:")
        self.assertContains(page, "Eintrag zurücknehmen")
        self.client.post(self.url, {"remove": "on"})
        self.client.post(self.url, {"remove": "on"})
        self.assertEqual(list(self.item.completed_by.all()), [self.owner])

    def test_permissions_and_scope(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.client.force_login(self.member)
        other = Event.objects.create(title="Andere")
        url = reverse("events:planning_item_participate", args=[other.pk, self.item.pk])
        self.assertEqual(self.client.post(url).status_code, 404)
        self.item.everyone = False
        self.item.save()
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.item.everyone = True
        self.item.save()
        self.event.set_access_password("secret")
        self.event.save()
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertFalse(self.item.completed_by.exists())

    def test_form_clears_individual_assignment_and_is_advert_only(self):
        form = PlanningItemForm({"category": "advert", "title": "Flyer ins Auto hängen", "everyone": "on", "people": "owner", "status": "done"}, category="advert")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["people"], "")
        self.assertEqual(form.cleaned_data["status"], "open")
        self.assertNotIn("everyone", PlanningItemForm(category="orga").fields)
        self.assertNotIn("everyone", PlanningItemForm(category="members").fields)

    def test_readonly_event_blocks_participation(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import EventDay
        self.event.auto_readonly = True
        self.event.save()
        EventDay.objects.create(event=self.event, date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertFalse(self.item.completed_by.exists())
        self.item.everyone = False
        self.item.save()
        url = reverse("events:planning_item_assign_self", args=[self.event.pk, self.item.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "")

    def test_template_copies_mode_but_not_participants(self):
        self.item.completed_by.add(self.member)
        target = Event.objects.create(title="Nächster Spieltag", planning_enabled=True, created_by=self.owner)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("events:planning_import", args=[target.pk]), {"source": self.event.pk, "reset_people": "on", "reset_status": "on"})
        self.assertEqual(response.status_code, 302)
        copied = target.planning_items.get()
        self.assertTrue(copied.everyone)
        self.assertFalse(copied.completed_by.exists())
        self.assertTrue(self.item.completed_by.exists())

    def test_status_tracks_participation_and_withdrawal(self):
        detail = reverse("events:event_detail", args=[self.event.pk])
        for user, payload, label, status in (
            (self.member, {"remove": "on"}, "Zum Mitmachen", "open"),
            (self.member, {}, "1 Person", "progress"),
            (self.owner, {}, "2 Personen", "progress"),
            (self.member, {"remove": "on"}, "1 Person", "progress"),
            (self.owner, {"remove": "on"}, "Zum Mitmachen", "open"),
        ):
            self.client.force_login(user)
            self.client.post(self.url, payload)
            page = self.client.get(detail)
            self.assertContains(page, label)
            self.assertContains(page, f'data-planning-status="{status}"')
            self.assertContains(page, f'class="pill {"warn" if status == "progress" else "danger"}">{label}')

    def test_self_assignment_preserves_people_and_prevents_duplicates(self):
        self.item.everyone = False
        self.item.people = "Anna"
        self.item.save()
        url = reverse("events:planning_item_assign_self", args=[self.event.pk, self.item.pk])
        detail = reverse("events:event_detail", args=[self.event.pk])
        self.assertContains(self.client.get(detail), "Ich übernehme das")
        for _ in range(2):
            self.assertEqual(self.client.post(url, {"people": "Someone else"}).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "Anna, member")
        self.assertEqual(self.item.revision, 1)
        self.assertEqual(self.event.orga_action_logs.count(), 1)
        self.assertContains(self.client.get(detail), "Du bist eingetragen.")
        self.client.force_login(self.owner)
        self.client.post(url)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "Anna, member, owner")

    def test_self_assignment_rejects_wrong_items_and_protected_events(self):
        url = reverse("events:planning_item_assign_self", args=[self.event.pk, self.item.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.item.everyone = False
        self.item.category = "members"
        self.item.save()
        self.assertEqual(self.client.post(url).status_code, 404)
        self.item.category = "advert"
        self.item.save()
        other = Event.objects.create(title="Other")
        self.assertEqual(self.client.post(reverse("events:planning_item_assign_self", args=[other.pk, self.item.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)
        self.client.force_login(self.member)
        self.event.set_access_password("secret")
        self.event.save()
        self.assertEqual(self.client.post(url).status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "")

    def test_self_assignment_handles_existing_name_and_full_field(self):
        self.item.everyone = False
        self.item.people = "Anna; member"
        self.item.save()
        url = reverse("events:planning_item_assign_self", args=[self.event.pk, self.item.pk])
        self.client.post(url)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "Anna; member")
        self.assertEqual(self.item.revision, 0)
        self.item.people = "x" * 300
        self.item.save()
        self.assertEqual(self.client.post(url).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "x" * 300)

    def test_responsibility_self_assignment(self):
        self.item.everyone = False
        self.item.category = "orga"
        self.item.people = "Anna"
        self.item.save()
        detail = reverse("events:event_detail", args=[self.event.pk])
        url = reverse("events:planning_item_assign_self", args=[self.event.pk, self.item.pk])
        self.assertContains(self.client.get(detail), "Ich übernehme das")
        for _ in range(2):
            self.assertEqual(self.client.post(url).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, "Anna, member")
        self.assertEqual(self.item.revision, 1)
        self.assertContains(self.client.get(detail), "Du bist eingetragen.")
        self.event.set_access_password("secret")
        self.event.save()
        self.assertEqual(self.client.post(url).status_code, 403)
