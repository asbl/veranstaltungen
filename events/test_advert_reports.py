from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from .models import Event, EventDay, PlanningItem


class AdvertReportTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='orga')
        self.member = get_user_model().objects.create_user(username='member')
        self.event = Event.objects.create(title='Spieltag', planning_enabled=True, created_by=self.owner)
        self.url = reverse('events:advert_report_create', args=[self.event.pk])
        self.client.force_login(self.member)
        self.data = {'include_self': 'on', 'title': 'Edeka Neu-Anspach', 'done': 'on', 'published_on': '2026-10-13', 'collaborators': 'S', 'notes': 'Flyer aufgehängt'}

    def test_member_can_report_without_management_rights(self):
        self.assertContains(self.client.get(reverse('events:event_detail', args=[self.event.pk])), 'Werbeort / Aktion hinzufügen')
        self.assertEqual(self.client.post(self.url, {**self.data, 'category': 'orga', 'everyone': 'on', 'people': 'forged'}).status_code, 302)
        item = self.event.planning_items.get()
        self.assertEqual((item.category, item.status, item.people, item.created_by), ('advert', 'done', 'member, S', self.member))
        self.assertFalse(item.everyone)
        self.assertEqual(str(item.published_on), '2026-10-13')
        self.assertIn('Flyer aufgehängt', item.notes)
        self.assertEqual(self.event.orga_action_logs.count(), 1)
        import os
        from pathlib import Path
        if os.environ.get("ADVERT_HTML_PATH"):
            Path(os.environ["ADVERT_HTML_PATH"]).write_text(self.client.get(reverse('events:event_detail', args=[self.event.pk])).content.decode())
            Path(os.environ["ADVERT_HTML_PATH"] + '.form').write_text(self.client.get(self.url).content.decode())
        self.assertEqual(self.client.post(reverse('events:planning_item_edit', args=[self.event.pk, item.pk]), self.data).status_code, 403)

    def test_planned_place_and_duplicate(self):
        self.assertEqual(self.client.post(self.url, {**self.data, 'done': ''}).status_code, 302)
        item = self.event.planning_items.get()
        self.assertEqual(item.status, 'open')
        self.assertIsNone(item.published_on)
        page = self.client.post(self.url, {**self.data, 'title': 'edeka neu-anspach'})
        self.assertContains(page, 'schon eingetragen')
        self.assertEqual(self.event.planning_items.count(), 1)

    def test_existing_report_preserves_data_and_detects_conflicts(self):
        item = PlanningItem.objects.create(event=self.event, category='advert', title='Edeka', people='Anna', notes='Vorhandene Notiz', reference_url='https://example.org')
        url = reverse('events:advert_report', args=[self.event.pk, item.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Edeka')
        self.assertEqual(self.client.post(url, {**self.data, 'revision': 0}).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.people, 'Anna, member, S')
        self.assertEqual(item.reference_url, 'https://example.org')
        self.assertTrue(item.notes.startswith('Vorhandene Notiz\n'))
        self.assertEqual(item.status, 'done')
        self.assertEqual(self.client.post(url, {**self.data, 'revision': 0}).status_code, 409)
        self.assertEqual(self.client.post(url, {**self.data, 'revision': 1, 'notes': ''}).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.people, 'Anna, member, S')
        other = Event.objects.create(title='Other', planning_enabled=True)
        self.assertEqual(self.client.post(reverse('events:advert_report', args=[other.pk, item.pk]), self.data).status_code, 404)
        item.everyone = True
        item.save()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_access_readonly_disabled_and_login(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, self.data).status_code, 302)
        self.client.force_login(self.member)
        self.event.set_access_password('secret')
        self.event.save()
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        self.event.set_access_password('')
        self.event.auto_readonly = True
        self.event.save()
        EventDay.objects.create(event=self.event, date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        self.event.auto_readonly = False
        self.event.list_settings = [{'key': 'advert', 'enabled': False}]
        self.event.save()
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        self.event.list_settings = []
        self.event.planning_enabled = False
        self.event.save()
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        self.assertFalse(self.event.planning_items.exists())

    def test_invalid_form_cannot_write(self):
        for data in ({**self.data, 'title': ''}, {**self.data, 'published_on': 'invalid'}, {**self.data, 'notes': 'x' * 2001}):
            self.assertEqual(self.client.post(self.url, data).status_code, 200)
        self.assertFalse(self.event.planning_items.exists())

    def test_planning_form_has_one_link_and_preserves_legacy_urls(self):
        from .forms import PlanningItemForm
        item = PlanningItem.objects.create(event=self.event, category='advert', title='Zeitung', publication_url='https://example.org/post')
        form = PlanningItemForm(instance=item, category='advert')
        self.assertNotIn('publication_url', form.fields)
        self.assertEqual(form['reference_url'].value(), 'https://example.org/post')
        data = {'title': 'Zeitung', 'status': 'done', 'revision': 0, 'reference_url': 'https://example.org/post'}
        form = PlanningItemForm(data, instance=item, category='advert')
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        item.refresh_from_db()
        self.assertEqual(item.reference_url, 'https://example.org/post')
        self.assertEqual(item.publication_url, '')
        item.publication_url = 'https://example.org/other'
        item.save()
        form = PlanningItemForm(data, instance=item, category='advert')
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        item.refresh_from_db()
        self.assertEqual(item.publication_url, 'https://example.org/other')
        self.client.force_login(self.owner)
        page = self.client.get(reverse('events:planning_item_create', args=[self.event.pk]) + '?category=advert')
        self.assertContains(page, 'Erweiterte Optionen')
        self.assertNotContains(page, 'id_publication_url')
        advanced = page.content.decode().split('<details class="form-more-options"')[1]
        for field in ('due_date', 'published_on', 'reference_url'):
            self.assertIn('id_' + field, advanced)


    def test_report_for_others_without_adding_reporter(self):
        self.assertContains(self.client.get(self.url), 'Mich selbst als verantwortlich / mitwirkend eintragen')
        response = self.client.post(self.url, {**self.data, 'include_self': '', 'collaborators': 'Sabine und Matthias'})
        self.assertEqual(response.status_code, 302)
        item = self.event.planning_items.get()
        self.assertEqual(item.people, 'Sabine, Matthias')
        self.assertIn('member: Flyer aufgehängt', item.notes)
        from .advert_score import highscore
        self.assertEqual({row['name']: row['points'] for row in highscore([item], [])}, {'Sabine': 1, 'Matthias': 1})
        url = reverse('events:advert_report', args=[self.event.pk, item.pk])
        self.assertEqual(self.client.post(url, {**self.data, 'include_self': '', 'collaborators': 'Anna', 'revision': item.revision}).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.people, 'Sabine, Matthias, Anna')

    def test_create_without_assignment_and_with_note(self):
        self.assertEqual(self.client.post(self.url, {**self.data, 'done': '', 'include_self': '', 'collaborators': ''}).status_code, 302)
        item = self.event.planning_items.get()
        self.assertEqual(item.people, '')
        self.assertEqual(item.status, 'open')
        self.assertIn('member: Flyer aufgehängt', item.notes)
