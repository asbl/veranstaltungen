from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from .models import Event, PlanningItem, AdvertIdentity
from .advert_score import highscore


class AdvertScoreTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='owner')
        self.member = get_user_model().objects.create_user(username='m.muster')
        self.event = Event.objects.create(title='Werbung', planning_enabled=True, created_by=self.owner)

    def test_groups_aliases_and_completed_actions(self):
        identity = AdvertIdentity.objects.create(event=self.event, name='Erika Muster', aliases='m.muster')
        for people, status in [('Sabine und Matthias, Erika Muster; m.muster', 'done'), ('Sabine & Matthias', 'done'), ('Offen', 'open')]:
            PlanningItem.objects.create(event=self.event, category='advert', title=people, people=people, status=status)
        item = PlanningItem.objects.create(event=self.event, category='advert', title='Status', everyone=True)
        item.completed_by.add(self.member)
        rows = highscore(self.event.planning_items.prefetch_related('completed_by__profile'), [identity])
        self.assertEqual({row['name']: row['points'] for row in rows}, {'Sabine': 2, 'Matthias': 2, 'Erika Muster': 2})
        self.assertEqual([row['rank'] for row in rows], [1, 1, 1])
        item.completed_by.clear()
        rows = highscore(self.event.planning_items.all(), [identity])
        self.assertEqual(next(row['points'] for row in rows if row['name'] == 'Erika Muster'), 1)
        self.client.force_login(self.owner)
        page = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        self.assertContains(page, 'Werbe-Highscore')
        self.assertContains(page, 'm.muster')

    def test_management_validation_and_scope(self):
        url = reverse('events:advert_aliases', args=[self.event.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url, {'name': 'Erika', 'aliases': 'm.muster'}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, {'name': 'Erika', 'aliases': 'm.muster'}).status_code, 302)
        self.assertContains(self.client.post(url, {'name': 'Andere', 'aliases': 'M.MUSTER'}), 'bereits einer anderen Person')
        self.assertEqual(self.event.advert_identities.count(), 1)
        identity = self.event.advert_identities.get()
        other = Event.objects.create(title='Andere', planning_enabled=True, created_by=self.owner)
        self.assertEqual(self.client.get(reverse('events:advert_alias_edit', args=[other.pk, identity.pk])).status_code, 404)
        edit = reverse('events:advert_alias_edit', args=[self.event.pk, identity.pk])
        self.assertEqual(self.client.post(edit, {'name': 'Erika Muster', 'aliases': 'm.muster; Erika'}).status_code, 302)
        self.assertEqual(self.client.post(edit, {'delete': '1'}).status_code, 302)
        self.assertFalse(self.event.advert_identities.exists())


    def test_compact_groups_and_collapsed_details(self):
        from html.parser import HTMLParser
        items = []
        for status in ('open', 'progress', 'waiting', 'done'):
            items.append(PlanningItem.objects.create(event=self.event, category='advert', title='Ort-' + status, status=status, people='Sabine und Matthias'))
        items.append(PlanningItem.objects.create(event=self.event, category='advert', title='Mitmachaktion', everyone=True))
        self.client.force_login(self.owner)
        page = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        self.assertContains(page, '<details class="advert-details">', html=False)
        self.assertEqual([len(group) for label, group in page.context['advert_status_groups']], [1, 2, 1])
        self.assertEqual(page.context['advert_everyone_items'], [items[-1]])
        html = page.content.decode()
        compact = html.split('<div class="advert-compact">')[1].split('<details class="advert-details">')[0]
        self.assertIn('Ich habe hier Werbung gemacht', compact)
        self.assertIn('aria-label="Bearbeiten: Ort-open"', compact)
        self.assertIn('aria-label="Löschen: Ort-open"', compact)
        class IdParser(HTMLParser):
            ids = []
            def handle_starttag(self, tag, attrs):
                self.ids.extend(value for name, value in attrs if name == 'id')
        parser = IdParser()
        parser.feed(html)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.client.force_login(self.member)
        html = self.client.get(reverse('events:event_detail', args=[self.event.pk])).content.decode()
        compact = html.split('<div class="advert-compact">')[1].split('<details class="advert-details">')[0]
        self.assertNotIn('advert-icon-action', compact)
        import os
        from pathlib import Path
        if os.environ.get('ADVERT_COMPACT_HTML'):
            Path(os.environ['ADVERT_COMPACT_HTML']).write_text(html)
