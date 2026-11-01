import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import IdeaComment, IdeaCommentNotification, get_display_name, safe_get_or_create

logger = logging.getLogger(__name__)

NOTIFICATION_DELAY = timezone.timedelta(hours=1)


def queue_idea_comment_notifications(comment):
    """Queue an email notification for everyone involved in this idea's
    discussion (the idea's author plus everyone who has commented on it),
    except the person who just wrote the comment. Actual sending happens
    later, batched, via send_pending_idea_comment_notifications."""
    idea = comment.idea
    recipients = {
        author_id
        for author_id in IdeaComment.objects.filter(idea=idea).values_list("author_id", flat=True)
    }
    recipients.add(idea.author_id)
    recipients.discard(None)
    recipients.discard(comment.author_id)

    for recipient_id in recipients:
        safe_get_or_create(IdeaCommentNotification, comment=comment, recipient_id=recipient_id)


def send_pending_idea_comment_notifications():
    """Send one digest email per recipient for all their pending notifications,
    but only once the oldest pending notification for that recipient is at
    least NOTIFICATION_DELAY old. Meant to be run periodically (e.g. every
    few minutes via cron), not on every comment.

    A failure sending one recipient's email (bad address, SMTP hiccup, ...)
    must not stop everyone else's digest from going out, so each recipient is
    handled independently and errors are only logged."""
    cutoff = timezone.now() - NOTIFICATION_DELAY
    pending = (
        IdeaCommentNotification.objects.filter(sent_at__isnull=True)
        .select_related("comment", "comment__idea", "comment__author", "comment__idea__event", "recipient")
        .order_by("created_at")
    )

    by_recipient = {}
    for notification in pending:
        by_recipient.setdefault(notification.recipient_id, []).append(notification)

    sent_count = 0
    for recipient_id, notifications in by_recipient.items():
        if notifications[0].created_at > cutoff:
            continue

        recipient = notifications[0].recipient
        if not recipient.email:
            continue

        try:
            _send_and_mark_sent(recipient, notifications)
        except Exception:
            logger.exception(
                "Ideen-Kommentar-Benachrichtigung an %s konnte nicht verschickt werden", recipient.email
            )
            continue

        sent_count += 1

    return sent_count


def _send_and_mark_sent(recipient, notifications):
    """Locks the notification rows so a second, overlapping run of this
    command (e.g. during a deploy) can't send the same digest twice, sends
    the email, then marks the rows sent - all in one transaction so a crash
    between sending and marking can't happen silently."""
    ids = [n.pk for n in notifications]
    with transaction.atomic():
        locked_ids = set(
            IdeaCommentNotification.objects.select_for_update(skip_locked=True)
            .filter(pk__in=ids, sent_at__isnull=True)
            .values_list("pk", flat=True)
        )
        if not locked_ids:
            return
        locked_notifications = [n for n in notifications if n.pk in locked_ids]
        _send_digest_email(recipient, locked_notifications)
        IdeaCommentNotification.objects.filter(pk__in=locked_ids).update(sent_at=timezone.now())


def _send_digest_email(recipient, notifications):
    entries = []
    for notification in notifications:
        comment = notification.comment
        idea = comment.idea
        url = "https://{}{}#idea-{}".format(
            settings.SITE_DOMAIN,
            reverse("events:event_detail", args=[idea.event_id]),
            idea.pk,
        )
        entries.append(
            {
                "idea": idea,
                "comment": comment,
                "author_name": get_display_name(comment.author) if comment.author else "Jemand",
                "url": url,
            }
        )

    subject = "Neue Kommentare zu deinen Ideen" if len(entries) > 1 else "Neuer Kommentar zu deiner Idee"
    body = render_to_string(
        "events/email/idea_comment_notification.txt",
        {"recipient_name": get_display_name(recipient), "entries": entries},
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient.email])
