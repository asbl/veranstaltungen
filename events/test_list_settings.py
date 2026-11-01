from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from .forms import EventForm
from .list_settings import LISTS
from .models import Event, PotluckItem


class ListSettingsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='lists', password='test')
        self.event = Event.objects.create(title='Listen-Test', created_by=self.user)
        self.client.force_login(self.user)

    def data(self):
        data = {'title': self.event.title, 'lists_configured': 'True',
                'list_order': ','.join(key for key, _, _ in reversed(LISTS))}
        for key, _, _ in LISTS:
            data[f'list_{key}_enabled'] = 'on'
        return data

    def test_defaults_and_planning(self):
        self.assertEqual(len(self.event.visible_lists), 7)
        self.event.planning_enabled = True
        self.assertEqual(len(self.event.visible_lists), 10)

    def test_save_render_order_hidden_and_escaped_description(self):
        item = PotluckItem.objects.create(event=self.event, title='Kuchen', created_by=self.user)
        data = self.data()
        del data['list_ideas_enabled']
        data['list_potluck_description'] = 'Rechnungen beim Kassenwart\n<script>alert(1)</script>'
        form = EventForm(data, instance=self.event)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.event.refresh_from_db()
        self.assertEqual(self.event.visible_lists[0]['key'], 'games')
        response = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertLess(html.index('id="games-section"'), html.index('id="potluck-section"'))
        self.assertNotIn('id="ideas-section"', html)
        self.assertNotIn('href="#ideas-section"', html)
        self.assertIn('Rechnungen beim Kassenwart<br>&lt;script&gt;', html)
        self.assertTrue(PotluckItem.objects.filter(pk=item.pk).exists())
        edit = self.client.get(reverse('events:event_edit', args=[self.event.pk]))
        self.assertContains(edit, 'data-list-settings')
        self.assertContains(edit, 'Rechnungen beim Kassenwart')

    def test_invalid_order_and_legacy_form(self):
        for order in ['games,games', 'unknown', '']:
            data = self.data()
            data['list_order'] = order
            self.assertFalse(EventForm(data, instance=self.event).is_valid())
        self.event.list_settings = [{'key': 'games', 'enabled': False, 'description': 'Behalten'}]
        self.event.save()
        form = EventForm({'title': 'Neuer Titel'}, instance=self.event)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.event.refresh_from_db()
        self.assertEqual(self.event.list_settings[0]['description'], 'Behalten')

    def test_edit_requires_manager(self):
        other = get_user_model().objects.create_user(username='other')
        self.client.force_login(other)
        response = self.client.post(reverse('events:event_edit', args=[self.event.pk]), self.data())
        self.assertEqual(response.status_code, 403)

    def test_responsibilities_status_setting_and_assignment_colors(self):
        from .models import PlanningItem
        self.event.planning_enabled = True
        self.event.save()
        assigned = PlanningItem.objects.create(event=self.event, title='Aufbau', people='Alex', status='done', created_by=self.user)
        PlanningItem.objects.create(event=self.event, title='Abbau', created_by=self.user)
        data = self.data()
        data['planning_enabled'] = 'on'
        form = EventForm(data, instance=self.event)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        response = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        section = response.content.decode().split('id="orga-section"')[1].split('</section>')[0]
        self.assertIn('Verantwortlichkeiten-Liste', section)
        self.assertNotIn('<th>Status</th>', section)
        self.assertNotIn('planning-status-filter', section)
        self.assertIn('responsibility-assigned', section)
        self.assertIn('responsibility-unassigned', section)
        url = reverse('events:planning_item_edit', args=[self.event.pk, assigned.pk])
        self.assertNotContains(self.client.get(url), 'name="status"')
        response = self.client.post(url, {'title': 'Aufbau', 'people': 'Sam', 'revision': assigned.revision, 'status': 'open'})
        self.assertEqual(response.status_code, 302)
        assigned.refresh_from_db()
        self.assertEqual(assigned.status, 'done')
        self.assertEqual(assigned.people, 'Sam')
        data['list_orga_show_status'] = 'on'
        form = EventForm(data, instance=self.event)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertContains(self.client.get(url), 'name="status"')
        section = self.client.get(reverse('events:event_detail', args=[self.event.pk])).content.decode().split('id="orga-section"')[1].split('</section>')[0]
        self.assertIn('<th>Status</th>', section)
        self.assertIn('planning-status-filter', section)
