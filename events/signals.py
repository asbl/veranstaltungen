from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from .models import ListAttachment


@receiver(post_delete, sender=ListAttachment)
def delete_attachment_file(sender, instance, **kwargs):
    if instance.file:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name))
