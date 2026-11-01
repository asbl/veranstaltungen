import base64
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from .forms import EventForm, EventDayFormSet
from .models import Event, ListAttachment

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


def upload(name='flyer.png', content=PNG):
    return SimpleUploadedFile(name, content)


class AttachmentTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = override_settings(MEDIA_ROOT=self.directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_user(username='attachments')
        self.event = Event.objects.create(title='Flyer', created_by=self.user, planning_enabled=True)
        self.client.force_login(self.user)

    def data(self):
        prefix = EventDayFormSet.get_default_prefix()
        return {'title': 'Flyer', 'planning_enabled': 'on', f'{prefix}-TOTAL_FORMS': '0',
                f'{prefix}-INITIAL_FORMS': '0', f'{prefix}-MIN_NUM_FORMS': '0', f'{prefix}-MAX_NUM_FORMS': '1000'}

    def file_url(self, attachment):
        return reverse('events:list_attachment_file', args=[attachment.event_id, attachment.pk])

    def test_multiple_uploads_render_and_download(self):
        data = self.data()
        data['list_advert_uploads'] = [upload(), upload('flyer.pdf', b'%PDF-1.7\nflyer')]
        response = self.client.post(reverse('events:event_edit', args=[self.event.pk]), data)
        self.assertEqual(response.status_code, 302)
        image, pdf = self.event.list_attachments.all()
        self.assertEqual(image.list_key, 'advert')
        self.assertTrue(Path(image.file.path).exists())
        response = self.client.get(reverse('events:event_detail', args=[self.event.pk]))
        self.assertContains(response, f'<img src="{self.file_url(image)}"')
        self.assertContains(response, 'flyer.pdf')
        self.assertContains(response, f'{self.file_url(pdf)}?download=1')
        for attachment, mime, disposition in [(image, 'image/png', 'inline'), (pdf, 'application/pdf', 'attachment')]:
            response = self.client.get(self.file_url(attachment))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], mime)
            self.assertTrue(response['Content-Disposition'].startswith(disposition))
            self.assertEqual(response['Cache-Control'], 'private, no-store')
            b"".join(response.streaming_content)
        response = self.client.get(self.file_url(image) + '?download=1')
        self.assertTrue(response['Content-Disposition'].startswith('attachment'))
        b"".join(response.streaming_content)

    def test_creation_and_failed_submission(self):
        data = self.data()
        data['list_advert_uploads'] = upload()
        response = self.client.post(reverse('events:event_create'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ListAttachment.objects.count(), 1)
        data['title'] = ''
        data['list_advert_uploads'] = upload()
        response = self.client.post(reverse('events:event_create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ListAttachment.objects.count(), 1)

    def test_validation_checks_every_file(self):
        for invalid in [upload('x.svg', b'<svg/>'), upload('fake.png', b'<html>bad</html>'),
                        upload('large.pdf', b'%PDF-' + b'0' * (20 * 1024 * 1024))]:
            form = EventForm({'title': 'Flyer'}, MultiValueDict({'list_advert_uploads': [invalid, upload()]}), instance=self.event)
            self.assertFalse(form.is_valid())
            self.assertIn('list_advert_uploads', form.errors)
        self.assertFalse(EventForm({'title': 'Flyer'}, MultiValueDict({'list_advert_uploads': [upload() for _ in range(11)]}), instance=self.event).is_valid())

    def test_remove_and_event_delete_clean_files(self):
        attachment = ListAttachment.objects.create(event=self.event, list_key='advert', file=upload(), original_name='flyer.png')
        path = Path(attachment.file.path)
        data = self.data()
        data['list_advert_remove'] = [str(attachment.pk)]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('events:event_edit', args=[self.event.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(path.exists())
        attachment = ListAttachment.objects.create(event=self.event, list_key='advert', file=upload(), original_name='flyer.png')
        path = Path(attachment.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            self.event.delete()
        self.assertFalse(path.exists())

    def test_access_and_foreign_removal(self):
        attachment = ListAttachment.objects.create(event=self.event, list_key='advert', file=upload(), original_name='flyer.png')
        self.event.set_access_password('secret')
        self.event.save()
        self.client.logout()
        self.assertEqual(self.client.get(self.file_url(attachment)).status_code, 403)
        self.client.post(reverse('events:event_detail', args=[self.event.pk]), {'access_password': 'secret'})
        response = self.client.get(self.file_url(attachment))
        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)
        self.event.list_settings = [{'key': 'advert', 'enabled': False}]
        self.event.save()
        self.assertEqual(self.client.get(self.file_url(attachment)).status_code, 404)
        self.client.force_login(self.user)
        response = self.client.get(self.file_url(attachment))
        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)
        other = Event.objects.create(title='Other', created_by=self.user)
        form = EventForm({'title': 'Other', 'list_advert_remove': [str(attachment.pk)]}, instance=other)
        self.assertFalse(form.is_valid())
        stranger = get_user_model().objects.create_user(username='stranger')
        self.client.force_login(stranger)
        data = self.data()
        data['list_advert_uploads'] = upload()
        self.assertEqual(self.client.post(reverse('events:event_edit', args=[self.event.pk]), data).status_code, 403)
