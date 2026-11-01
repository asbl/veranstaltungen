"""Validation and private storage paths for list attachments."""
from pathlib import Path
from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError

MAX_FILE_SIZE = 20 * 1024 * 1024
CONTENT_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                 '.gif': 'image/gif', '.webp': 'image/webp', '.pdf': 'application/pdf'}


def attachment_path(instance, filename):
    return f'list-attachments/{instance.event_id}/{uuid4().hex}{Path(filename).suffix.lower()}'


def validate_attachment(upload):
    suffix = Path(upload.name).suffix.lower()
    if suffix not in CONTENT_TYPES:
        raise ValidationError('Bitte PNG, JPEG, GIF, WebP oder PDF hochladen.')
    if upload.size > MAX_FILE_SIZE:
        raise ValidationError('Eine Datei darf höchstens 20 MB groß sein.')
    header = upload.read(16)
    upload.seek(0)
    matches = {
        '.png': header.startswith(b'\x89PNG\r\n\x1a\n'),
        '.jpg': header.startswith(b'\xff\xd8\xff'),
        '.jpeg': header.startswith(b'\xff\xd8\xff'),
        '.gif': header.startswith((b'GIF87a', b'GIF89a')),
        '.webp': header[:4] == b'RIFF' and header[8:12] == b'WEBP',
        '.pdf': header.startswith(b'%PDF-'),
    }
    if not matches[suffix]:
        raise ValidationError('Dateiinhalt und Dateiendung passen nicht zusammen.')


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class AttachmentField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data] if data else []
        if len(files) > 10:
            raise ValidationError('Bitte höchstens 10 Dateien pro Liste auf einmal hochladen.')
        return [super(AttachmentField, self).clean(file, initial) for file in files]
