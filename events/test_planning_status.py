from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from .models import Event, PlanningItem


class PlanningStatusTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='orga')
        self.member = get_user_model().objects.create_user(username='member')
        self.event = Event.objects.create(title='Test', planning_enabled=True, created_by=self.owner)
        self.item = PlanningItem.objects.create(event=self.event, category='advert', title='Flyer', people='member', notes='Behalten')
        self.url = reverse('events:planning_status_edit', args=[self.event.pk, self.item.pk])
        self.client.force_login(self.member)

    def test_responsible_can_change_only_status_with_conflict_protection(self):
        page = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        self.assertContains(page, self.url)
        self.assertContains(page, 'class="advert-report-link"')
        self.assertNotContains(page, 'class="btn advert-report-btn"')
        self.assertEqual(self.client.get(self.url).status_code, 200)
        response = self.client.post(self.url, {'status': 'progress', 'revision': 0, 'title': 'Forged', 'people': 'Forged'})
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual((self.item.status, self.item.title, self.item.people, self.item.notes), ('progress', 'Flyer', 'member', 'Behalten'))
        self.assertEqual(self.item.revision, 1)
        self.assertEqual(self.event.orga_action_logs.count(), 1)
        self.assertEqual(self.client.post(self.url, {'status': 'done', 'revision': 0}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'status': 'invalid', 'revision': 1}).status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'progress')

    def test_permissions_category_and_event_scope(self):
        self.item.people = ''
        self.item.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.item.created_by = self.member
        self.item.save()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.item.category = 'members'
        self.item.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url, {'status': 'done', 'revision': 0}).status_code, 302)
        other = Event.objects.create(title='Other', planning_enabled=True, created_by=self.owner)
        self.assertEqual(self.client.get(reverse('events:planning_status_edit', args=[other.pk, self.item.pk])).status_code, 404)
        self.item.everyone = True
        self.item.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_access_readonly_and_disabled_lists(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import EventDay
        self.event.set_access_password('secret')
        self.event.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.event.set_access_password('')
        self.event.auto_readonly = True
        self.event.save()
        EventDay.objects.create(event=self.event, date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.event.auto_readonly = False
        self.event.list_settings = [{'key': 'advert', 'enabled': False}]
        self.event.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_people_popup_claim_and_conflict(self):
        self.item.people = ""
        self.item.save()
        url = reverse('events:planning_people_edit', args=[self.event.pk, self.item.pk])
        page = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        self.assertContains(page, url)
        response = self.client.get(url, HTTP_X_PLANNING_POPUP='1')
        self.assertContains(response, 'data-planning-field-form')
        self.assertNotContains(response, '<html')
        response = self.client.post(url, {'people': 'member, S', 'revision': 0, 'status': 'done'}, HTTP_X_PLANNING_POPUP='1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('#planning-', response.json()['location'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, 'member, S')
        self.assertEqual(self.item.status, 'open')
        self.assertEqual(self.item.revision, 1)
        self.assertEqual(self.event.orga_action_logs.count(), 1)
        response = self.client.post(url, {'people': 'Meine Eingabe', 'revision': 0}, HTTP_X_PLANNING_POPUP='1')
        self.assertContains(response, 'Meine Eingabe', status_code=409)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, 'member, S')
        response = self.client.post(url, {'people': 'x' * 301, 'revision': 1}, HTTP_X_PLANNING_POPUP='1')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(url, {'people': '', 'revision': 1}, HTTP_X_PLANNING_POPUP='1').status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.people, '')

    def test_people_permission_and_password_guest_login(self):
        url = reverse('events:planning_people_edit', args=[self.event.pk, self.item.pk])
        self.item.people = 'Other person'
        self.item.save()
        self.assertEqual(self.client.post(url, {'people': 'member', 'revision': 0}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.item.everyone = True
        self.item.save()
        self.assertEqual(self.client.get(url).status_code, 403)
        self.item.everyone = False
        self.item.save()
        self.client.logout()
        self.event.set_access_password('secret')
        self.event.save()
        from .views import grant_event_access
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.session = self.client.session
        grant_event_access(request, self.event)
        request.session.save()
        page = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        self.assertContains(page, url)
        self.assertContains(page, self.url)
        response = self.client.get(url, HTTP_X_PLANNING_POPUP='1')
        self.assertEqual(response.status_code, 302)
        self.assertIn('next=', response.url)

    def test_popup_status_validation_and_fixture(self):
        page = self.client.get(self.url, HTTP_X_PLANNING_POPUP='1')
        self.assertContains(page, 'data-planning-field-form')
        self.assertNotContains(page, '<html')
        response = self.client.post(self.url, {'status': 'invalid', 'revision': 0}, HTTP_X_PLANNING_POPUP='1')
        self.assertEqual(response.status_code, 400)
        import os
        if os.environ.get('PLANNING_POPUP_FIXTURE'):
            import json
            from pathlib import Path
            people_url = reverse('events:planning_people_edit', args=[self.event.pk, self.item.pk])
            self.item.people = ''
            self.item.save()
            self.client.force_login(self.owner)
            Path(os.environ['PLANNING_POPUP_FIXTURE']).write_text(json.dumps({
                'page': self.client.get(reverse('events:event_detail', args=[self.event.pk])).content.decode(),
                'status_url': self.url,
                'people_url': people_url,
                'status': page.content.decode(),
                'people': self.client.get(people_url, HTTP_X_PLANNING_POPUP='1').content.decode(),
                'invalid': response.content.decode(),
            }))
        response = self.client.post(self.url, {'status': 'done', 'revision': 0}, HTTP_X_PLANNING_POPUP='1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('location', response.json())
