import hashlib
import itertools
import re
import secrets
from datetime import date
from urllib.parse import quote

import openpyxl
from allauth.account.adapter import get_adapter
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .advert_score import highscore, split_names
from . import bgg
from . import translations
from .account_forms import EventAccessLoginCodeForm, initiate_login_code
from .game_recommendations import MAX_PER_GROUP, game_recommendations
from .planning import todo_groups
from .qr import qr_svg
from .forms import (
    AdvertReportForm,
    AnswerForm,
    AttendanceForm,
    BroughtPersonFormSet,
    DeclineForm,
    DutyForm,
    DutyScheduleForm,
    PlanningItemForm,
    PlanningStatusForm,
    PlanningPeopleForm,
    EventDayFormSet,
    EventForm,
    EventOrgaForm,
    GameCommentForm,
    GameEntryForm,
    GameBulkForm,
    BggTranslationForm,
    IdeaCommentForm,
    IdeaForm,
    PotluckClaimForm,
    PotluckOrgaClaimForm,
    PotluckItemForm,
    QuestionForm,
    SlotCommentForm,
    SlotForm,
)
from .models import (
    Answer,
    AnswerLike,
    AnswerReaction,
    Attendance,
    BroughtPerson,
    BggTranslation,
    Decline,
    Duty,
    DutySignup,
    Event,
    EventDay,
    ListAttachment,
    EventVisit,
    GameBringClaim,
    GameExplainClaim,
    GameComment,
    GameEntry,
    GameInterest,
    Idea,
    IdeaComment,
    IdeaLike,
    is_known_person,
    OrgaActionLog,
    PlanningItem,
    PotluckClaim,
    PotluckItem,
    Question,
    QuestionLike,
    QuestionReaction,
    REACTION_EMOJI_CHOICES,
    SiteSettings,
    Slot,
    SlotComment,
    SlotSignup,
    UserProfile,
    get_display_name,
    safe_get_or_create,
)
from .notifications import queue_idea_comment_notifications


EVENT_ACCESS_TTL = getattr(settings, "EVENT_ACCESS_TTL", 60 * 60 * 24)
EVENT_ACCESS_MAX_ATTEMPTS = getattr(settings, "EVENT_ACCESS_MAX_ATTEMPTS", 5)
EVENT_ACCESS_ATTEMPT_WINDOW = getattr(settings, "EVENT_ACCESS_ATTEMPT_WINDOW", 60 * 15)
MAX_RECENT_EVENT_CHANGES = 100


def _log_orga_action(request, event, description):
    """Protokolliert Planungsänderungen und stellvertretende Orga-Aktionen."""
    OrgaActionLog.objects.create(event=event, actor=request.user, description=description)


def _cleanup_attendee_commitments(event, user):
    """When someone stops attending, clear the commitments other people
    might be relying on (potluck items, games, duties, workshop slots) so
    nothing is silently left dangling right before the event."""
    removed = 0
    removed += PotluckClaim.objects.filter(item__event=event, user=user).delete()[0]
    removed += GameBringClaim.objects.filter(game__event=event, user=user).delete()[0]
    removed += GameExplainClaim.objects.filter(game__event=event, user=user).delete()[0]
    removed += SlotSignup.objects.filter(slot__event=event, user=user).delete()[0]
    removed += DutySignup.objects.filter(duty__event=event, user=user).delete()[0]
    return removed


def _change_text(value, length=90):
    text = " ".join(str(value).split())
    return text if len(text) <= length else f"{text[:length - 1]}…"


def _recent_event_changes(event, since, user=None):
    """Return bounded, user-facing activity created by other users since a visit."""
    if since is None:
        return []

    groups = []
    item_count = 0

    def add(label, items):
        nonlocal item_count
        remaining = MAX_RECENT_EVENT_CHANGES - item_count
        if remaining <= 0:
            return
        items = list(itertools.islice(items, remaining))
        if items:
            groups.append({"label": label, "items": items})
            item_count += len(items)

    def others(queryset, field):
        return queryset.exclude(**{field: user}) if user is not None else queryset

    add(
        "Neue Anmeldungen",
        (
            attendance.display_name
            for attendance in others(Attendance.objects.filter(event=event, created_at__gt=since), "user")
            .select_related("user__profile")
            .order_by("created_at")
        ),
    )
    add(
        "Neue mitgebrachte Personen",
        (
            f"{person.name} (über {person.attendance.display_name})"
            for person in others(BroughtPerson.objects.filter(attendance__event=event, created_at__gt=since), "attendance__user")
            .select_related("attendance__user__profile")
            .order_by("created_at")
        ),
    )
    add(
        "Neue Absagen",
        (
            get_display_name(decline.user)
            for decline in others(Decline.objects.filter(event=event, created_at__gt=since), "user")
            .select_related("user__profile")
            .order_by("created_at")
        ),
    )

    comments = []
    comments.extend(
        (
            comment.created_at,
            f"{get_display_name(comment.author) if comment.author else 'Unbekannt'} zu Idee „{_change_text(comment.idea.text, 45)}“: {_change_text(comment.text)}",
        )
        for comment in others(IdeaComment.objects.filter(idea__event=event, created_at__gt=since), "author")
        .select_related("author__profile", "idea")
    )
    comments.extend(
        (
            comment.created_at,
            f"{get_display_name(comment.author) if comment.author else 'Unbekannt'} zu „{comment.slot.title}“: {_change_text(comment.text)}",
        )
        for comment in others(SlotComment.objects.filter(slot__event=event, created_at__gt=since), "author")
        .select_related("author__profile", "slot")
    )
    comments.extend(
        (
            comment.created_at,
            f"{get_display_name(comment.author) if comment.author else 'Unbekannt'} zu „{comment.game.name}“: {_change_text(comment.text)}",
        )
        for comment in others(GameComment.objects.filter(game__event=event, created_at__gt=since), "author")
        .select_related("author__profile", "game")
    )
    add("Neue Kommentare", (text for _, text in sorted(comments, key=lambda item: item[0])))

    add(
        "Neue Ideen",
        (_change_text(idea.text) for idea in others(Idea.objects.filter(event=event, created_at__gt=since), "author").order_by("created_at")),
    )
    add(
        "Neue Fragen",
        (_change_text(question.text) for question in others(Question.objects.filter(event=event, created_at__gt=since), "author").order_by("created_at")),
    )
    add(
        "Neue Antworten",
        (
            f"Zu „{_change_text(answer.question.text, 45)}“: {_change_text(answer.text)}"
            for answer in others(Answer.objects.filter(question__event=event, created_at__gt=since), "author")
            .select_related("question")
            .order_by("created_at")
        ),
    )
    add(
        "Neue Spiele",
        (game.name for game in others(GameEntry.objects.filter(event=event, created_at__gt=since), "created_by").order_by("created_at")),
    )
    add(
        "Neue Mitbring-Zusagen für Spiele",
        (
            f"{get_display_name(claim.user)} bringt „{claim.game.name}“ mit"
            for claim in others(GameBringClaim.objects.filter(game__event=event, created_at__gt=since), "user")
            .select_related("user__profile", "game")
            .order_by("created_at")
        ),
    )
    add(
        "Neue Einträge auf der Mitbringliste",
        (item.title for item in others(PotluckItem.objects.filter(event=event, created_at__gt=since), "created_by").order_by("created_at")),
    )
    add(
        "Neue Mitbring-Zusagen",
        (
            f"{claim.person_label}: {claim.item.format_quantity(claim.quantity)} × {claim.item.title}"
            for claim in others(others(PotluckClaim.objects.filter(item__event=event, created_at__gt=since), "user"), "entered_by")
            .select_related("user__profile", "item")
            .order_by("created_at")
        ),
    )
    add(
        "Neue Workshops/Angebote",
        (slot.title for slot in others(Slot.objects.filter(event=event, created_at__gt=since), "created_by").order_by("created_at")),
    )
    add(
        "Neue Workshop-Anmeldungen",
        (
            f"{get_display_name(signup.user)} für „{signup.slot.title}“"
            for signup in others(SlotSignup.objects.filter(slot__event=event, created_at__gt=since), "user")
            .select_related("user__profile", "slot")
            .order_by("created_at")
        ),
    )
    add(
        "Neue Dienste",
        (duty.title for duty in others(Duty.objects.filter(event=event, created_at__gt=since), "created_by").order_by("created_at")),
    )
    add(
        "Neue Dienst-Anmeldungen",
        (
            f"{get_display_name(signup.user)} für „{signup.duty.title}“"
            for signup in others(DutySignup.objects.filter(duty__event=event, created_at__gt=since), "user")
            .select_related("user__profile", "duty")
            .order_by("created_at")
        ),
    )
    return groups


LOGO_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def site_logo(request):
    """Öffentliches Logo des Betreibers aus den SiteSettings (MEDIA_ROOT).

    Das Logo ist öffentlich (Branding im Header), daher bewusst ohne
    Veranstaltungs-Zugangsschutz; nur Bildtypen werden ausgeliefert.
    """
    from pathlib import Path

    site_settings = SiteSettings.load()
    if not site_settings.logo:
        raise Http404
    suffix = Path(site_settings.logo.name).suffix.lower()
    content_type = LOGO_CONTENT_TYPES.get(suffix)
    if not content_type:
        raise Http404
    try:
        logo_file = site_settings.logo.open("rb")
    except FileNotFoundError:
        raise Http404
    response = FileResponse(logo_file, content_type=content_type)
    response["Cache-Control"] = "public, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def event_list(request):
    """Kommende Veranstaltungen zuerst (nach erstem Tag aufsteigend, Events
    ohne Tag ans Ende), danach die beendeten (nach letztem Tag absteigend).
    Event.is_finished nutzt dieselbe Regel wie der Schreibschutz, daher ohne
    zusätzliche Abfrage gruppierbar (days sind geprefetcht)."""
    upcoming_events, past_events = [], []
    for event in Event.objects.prefetch_related("days").all():
        (past_events if event.is_finished else upcoming_events).append(event)

    def day_sort_key(event, default, pick):
        dates = [day.date for day in event.days.all()]
        return pick(dates) if dates else default

    upcoming_events.sort(key=lambda event: day_sort_key(event, date.max, min))
    past_events.sort(key=lambda event: day_sort_key(event, date.min, max), reverse=True)
    return render(
        request,
        "events/event_list.html",
        {"upcoming_events": upcoming_events, "past_events": past_events},
    )


def _event_access_token(request):
    """Stabiler, selbst vergebener Session-Anker für die Freischaltung.

    request.session.session_key ist dafür ungeeignet: django.contrib.auth.login()
    ruft beim Anmelden cycle_key() auf und vergibt damit eine neue Session-ID
    (Schutz vor Session-Fixation), behält aber die Session-Daten bei. Wird also
    zwischen "Passwort eingeben" und "per E-Mail-Code einloggen" (z.B. um sich
    anzumelden) der Session-Key gewechselt, ginge eine an den alten Key
    gebundene Freischaltung sonst verloren und die nächste geschützte Aktion
    würde fälschlich mit 403 abgelehnt."""
    token = request.session.get("event_access_token")
    if not token:
        token = secrets.token_hex(16)
        request.session["event_access_token"] = token
    return token


def _event_access_cache_key(request, event):
    """Freischaltung ist an die Session gebunden, nicht an den Nutzer - das
    Passwort dient nur als Zugangsschutz für die Veranstaltungsseite, nicht
    als personenbezogenes Login."""
    password_version = hashlib.sha256(event.access_password_hash.encode()).hexdigest()[:16]
    return f"event-access:{event.pk}:{password_version}:{_event_access_token(request)}"


def _event_access_attempt_key(request, event):
    """Return a privacy-preserving rate-limit key for this client and event."""
    client_ip = request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR", "unknown")
    client_fingerprint = hashlib.sha256(client_ip.encode()).hexdigest()[:24]
    return f"event-access-attempts:{event.pk}:{client_fingerprint}"


def _failed_event_access_attempts(request, event):
    return int(cache.get(_event_access_attempt_key(request, event), 0))


def _record_failed_event_access_attempt(request, event):
    key = _event_access_attempt_key(request, event)
    if cache.add(key, 1, timeout=EVENT_ACCESS_ATTEMPT_WINDOW):
        return 1
    try:
        attempts = cache.incr(key)
        # BaseCache.incr() would otherwise fall back to the backend's default
        # timeout. Keep the configured lockout window explicit.
        cache.set(key, attempts, timeout=EVENT_ACCESS_ATTEMPT_WINDOW)
        return attempts
    except ValueError:
        # The key can expire between add() and incr(). Start a fresh window.
        cache.set(key, 1, timeout=EVENT_ACCESS_ATTEMPT_WINDOW)
        return 1


def has_event_access(request, event):
    if not event.has_access_password:
        return True
    # Orga/Admin sind bereits über can_manage vertrauenswürdig autorisiert -
    # sie sollen nicht zusätzlich das Zugangspasswort ihrer eigenen (oder
    # ihnen anvertrauten) Veranstaltung eingeben müssen.
    if event.can_manage(request.user):
        return True
    # Bekannte Personen dürfen bei entsprechend markierten Veranstaltungen
    # ohne Zugangspasswort hinein: Ein Login per E-Mail-Code genügt (siehe
    # event_access_code_request und Event.allow_known_persons_signup).
    if event.allow_known_persons_signup and is_known_person(request.user):
        return True
    return bool(cache.get(_event_access_cache_key(request, event)))


def grant_event_access(request, event):
    cache.set(_event_access_cache_key(request, event), True, timeout=EVENT_ACCESS_TTL)
    cache.delete(_event_access_attempt_key(request, event))


def _require_event_access(request, event):
    """Gate für alle Schreib-Endpunkte einer (ggf. passwortgeschützten)
    Veranstaltung - nicht nur für event_detail selbst. Ohne diesen Check
    ließe sich der Passwortschutz umgehen, indem man direkt auf die
    Unter-Endpunkte (Idee anlegen, RSVP, Kommentar, ...) postet, ohne die
    Detailseite je aufgerufen/das Passwort eingegeben zu haben.

    Zusätzlich greift hier der Schreibschutz beendeter Veranstaltungen
    (Event.auto_readonly): Alles bleibt einsehbar, Änderungen sind nur noch
    der Orga möglich, die so auch nachträglich korrigieren und den Schreibschutz
    selbst wieder aufheben kann. Einzige Ausnahme ist der Anzeigename
    (Kontodaten, siehe set_display_name)."""
    if not has_event_access(request, event):
        raise PermissionDenied
    if event.is_readonly and not event.can_manage(request.user):
        raise PermissionDenied


def _tally_reactions(obj, user):
    counts = {}
    mine = set()
    for r in obj.reactions.all():
        counts[r.emoji] = counts.get(r.emoji, 0) + 1
        if user.is_authenticated and r.user_id == user.pk:
            mine.add(r.emoji)
    obj.reaction_tally = counts
    obj.my_reactions = mine
    obj.liked_by_me = user.is_authenticated and any(like.user_id == user.pk for like in obj.likes.all())


def _event_user_profiles(event):
    """Build the event-scoped commitments shown in the user popup.

    The event detail view already prefetches these relations, so this adds no
    per-user queries and exposes no account data such as usernames or email.
    """
    profiles = {}

    def profile_for(user_id):
        return profiles.setdefault(
            str(user_id),
            {"potluck": [], "games": [], "duties": []},
        )

    for item in event.potluck_items.all():
        for claim in item.claims.all():
            unit = getattr(item, "unit", "")
            quantity = f"{claim.quantity} {unit}" if unit else str(claim.quantity)
            text = f"{quantity} × {item.title}"
            if claim.comment:
                text += f" ({claim.comment})"
            if claim.user_id:
                profile_for(claim.user_id)["potluck"].append(text)

    for game in event.games.all():
        for claim in game.bring_claims.all():
            profile_for(claim.user_id)["games"].append(game.name)

    for duty in event.duties.all():
        schedule = []
        if duty.day:
            schedule.append(duty.day.display_label)
        if duty.start_time or duty.end_time:
            start = duty.start_time.strftime("%H:%M") if duty.start_time else ""
            end = duty.end_time.strftime("%H:%M") if duty.end_time else ""
            schedule.append(f"{start}–{end} Uhr" if end else f"{start} Uhr")
        text = duty.title
        if schedule:
            text += f" – {', '.join(schedule)}"
        for signup in duty.signups.all():
            profile_for(signup.user_id)["duties"].append(text + (" (Springer)" if getattr(signup, "is_reserve", False) else ""))

    return profiles


def _duty_groups(event, user=None):
    """Dienste der Detailseite aufbereiten: Der Dienstplan wird nach Zeitraum
    gruppiert und chronologisch sortiert; je Zeitraum werden die Dienste als
    kompakte Zeilen angezeigt und die Springer nur einmal (Raster-Dienst
    „Springer“ und Springer-Anmeldungen der übrigen Dienste zusammen).
    Auf-/Abbau bleiben eine flache, nach Tag und Uhrzeit sortierte Liste.
    Mit ``user`` wird markiert, wo die Person selbst eingetragen ist."""
    user_id = user.pk if user is not None and user.is_authenticated else None
    by_category = {}
    for duty in event.duties.all():
        duty.regular_signups = []
        duty.reserve_signups = []
        for signup in duty.signups.all():
            (duty.reserve_signups if signup.is_reserve else duty.regular_signups).append(signup)
        duty.signed_up_by_me = user_id is not None and any(
            signup.user_id == user_id for signup in duty.regular_signups
        )
        by_category.setdefault(duty.category, []).append(duty)

    def sort_key(duty):
        return (
            duty.day.date.isoformat() if duty.day else "9999-12-31",
            duty.start_time.strftime("%H:%M") if duty.start_time else "99:99",
            duty.end_time.strftime("%H:%M") if duty.end_time else "",
            duty.title.lower(),
        )

    def slot_label(duty, show_day):
        if duty.start_time and duty.end_time:
            label = f"{duty.start_time:%H:%M}–{duty.end_time:%H:%M} Uhr"
        elif duty.start_time:
            label = f"ab {duty.start_time:%H:%M} Uhr"
        elif duty.end_time:
            label = f"bis {duty.end_time:%H:%M} Uhr"
        else:
            label = "Ohne feste Zeit"
        if show_day and duty.day:
            label = f"{duty.day.display_label} · {label}"
        return label

    groups = []
    for category, label in Duty._meta.get_field("category").choices:
        duties = sorted(by_category.get(category, []), key=sort_key)
        if category != "service":
            groups.append((category, label, duties))
            continue
        show_day = len({d.day_id for d in duties if d.day_id}) > 1
        by_slot = {}
        for duty in duties:
            by_slot.setdefault((duty.day_id, duty.start_time, duty.end_time), []).append(duty)
        slots = []
        for slot_duties in by_slot.values():
            springer_duties = [d for d in slot_duties if d.title == "Springer"]
            regular = [d for d in slot_duties if d.title != "Springer"]
            springer_signups = [s for d in springer_duties for s in d.regular_signups + d.reserve_signups]
            springer_signups += [s for d in regular for s in d.reserve_signups]
            # Springer-Anmeldung: auf den Raster-Springer-Dienst, sonst als
            # Springer (is_reserve) auf den ersten Dienst des Zeitraums.
            target = springer_duties[0] if springer_duties else regular[0]
            slots.append({
                "label": slot_label(slot_duties[0], show_day),
                "sort": sort_key(slot_duties[0]),
                "duties": regular,
                "springer_duty": springer_duties[0] if springer_duties else None,
                "springer_signups": springer_signups,
                "springer_mine": user_id is not None and any(s.user_id == user_id for s in springer_signups),
                "springer_target": target,
                "springer_is_reserve": not springer_duties,
            })
        slots.sort(key=lambda slot: slot["sort"])
        groups.append((category, label, slots))
    return groups


def _render_event_password(request, event, status=None):
    """Zugangsseite: das bisherige Passwort-Freischalten und - bei
    Veranstaltungen mit „Bekannte Personen dürfen sich ohne Schlüssel
    anmelden“ - zusätzlich das Formular für die Anmeldung per E-Mail-Code."""
    context = {"event": event}
    if event.allow_known_persons_signup:
        context["code_form"] = EventAccessLoginCodeForm()
    if status is not None:
        return render(request, "events/event_password.html", context, status=status)
    return render(request, "events/event_password.html", context)


def game_explain_print(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if not has_event_access(request, event):
        return redirect("events:event_detail", pk=event.pk)
    attending_user_ids = set(event.attendances.values_list("user_id", flat=True))
    games = sorted(
        event.games.prefetch_related("explain_claims__user__profile"),
        key=lambda game: game.name.casefold(),
    )
    translations.attach_translations(games)
    for game in games:
        for claim in game.explain_claims.all():
            claim.user_is_attending = event.planning_enabled or claim.user_id in attending_user_ids
        if game.bgg_id:
            game.bgg_url = f"https://boardgamegeek.com/boardgame/{game.bgg_id}"
            game.bgg_qr = qr_svg(game.bgg_url, f"QR-Code: {game.name} auf BoardGameGeek")
    return render(request, "events/game_explain_print.html", {
        "event": event,
        "games": games,
        "recommendations": game_recommendations(games),
        "recommendation_limit": MAX_PER_GROUP,
    })


def _potluck_items(event, attending_user_ids, attendee_count, user=None):
    """Mitbringliste für Detail- und Druckansicht: Anmeldestatus je Zusage
    setzen und offene Einträge zuerst, sonst alphabetisch sortieren."""
    user_id = user.pk if user is not None and user.is_authenticated else None
    for item in event.potluck_items.all():
        item.attendee_count = attendee_count
        item.claimed_by_me = user_id is not None and any(
            claim.user_id == user_id for claim in item.claims.all()
        )
        for claim in item.claims.all():
            # Ohne Konto eingetragene Zusagen (nur durch die Orga) zählen immer.
            claim.user_is_attending = (
                event.planning_enabled or claim.user_id is None or claim.user_id in attending_user_ids
            )
    return sorted(event.potluck_items.all(), key=lambda item: (not item.is_missing, item.title.lower()))


def potluck_print(request, pk):
    event = get_object_or_404(
        Event.objects.prefetch_related("potluck_items__claims__user__profile"), pk=pk
    )
    if not has_event_access(request, event):
        return redirect("events:event_detail", pk=event.pk)
    attending_user_ids = set(event.attendances.values_list("user_id", flat=True))
    attendee_count = event.attendances.count() + BroughtPerson.objects.filter(attendance__event=event).count()
    items = _potluck_items(event, attending_user_ids, attendee_count)
    return render(request, "events/potluck_print.html", {"event": event, "items": items})


def duty_print(request, pk):
    event = get_object_or_404(
        Event.objects.prefetch_related("duties__day", "duties__signups__user__profile"), pk=pk
    )
    if not has_event_access(request, event):
        return redirect("events:event_detail", pk=event.pk)
    attending_user_ids = set(event.attendances.values_list("user_id", flat=True))
    for duty in event.duties.all():
        # Freie Plätze werden als Leerzeilen zum Eintragen per Hand gedruckt.
        duty.open_spots = range(duty.spots_left or 0)
        for signup in duty.signups.all():
            signup.user_is_attending = event.planning_enabled or signup.user_id in attending_user_ids
    return render(request, "events/duty_print.html", {
        "event": event,
        "duty_groups": _duty_groups(event),
        "has_duties": bool(event.duties.all()),
    })


def event_detail(request, pk):
    gate_event = get_object_or_404(Event, pk=pk)
    if not has_event_access(request, gate_event):
        if request.method == "POST":
            if _failed_event_access_attempts(request, gate_event) >= EVENT_ACCESS_MAX_ATTEMPTS:
                messages.error(request, "Zu viele Fehlversuche. Bitte versuche es später erneut.")
                return _render_event_password(request, gate_event, status=429)
            entered = request.POST.get("access_password", "")
            if entered and gate_event.check_access_password(entered):
                grant_event_access(request, gate_event)
                if request.resolver_match.url_name == "idea_permalink":
                    return redirect(request.path)
                return redirect("events:event_detail", pk=gate_event.pk)
            failed_attempts = _record_failed_event_access_attempt(request, gate_event)
            messages.error(request, "Falsches Passwort.")
            if failed_attempts >= EVENT_ACCESS_MAX_ATTEMPTS:
                messages.error(request, "Zu viele Fehlversuche. Bitte versuche es später erneut.")
                return _render_event_password(request, gate_event, status=429)
        return _render_event_password(request, gate_event)

    event = get_object_or_404(
        Event.objects.prefetch_related(
            "list_attachments",
            "days",
            "attendances__user__profile",
            "attendances__days",
            "attendances__brought_people__days",
            "declines__user__profile",
            "potluck_items__created_by__profile",
            "potluck_items__claims__user__profile",
            "slots__signups__user__profile",
            "slots__comments__author__profile",
            "games__created_by__profile",
            "games__bring_claims__user__profile",
            "games__explain_claims__user__profile",
            "games__comments__author__profile",
            "games__interested_users__user__profile",
            "duties__day",
            "duties__created_by__profile",
            "duties__signups__user__profile",
        ),
        pk=pk,
    )
    recent_changes = []
    recent_change_count = 0
    first_tracked_visit = False
    if request.user.is_authenticated:
        with transaction.atomic():
            visit, first_tracked_visit = safe_get_or_create(
                EventVisit,
                event=event,
                user=request.user,
                defaults={"last_visited_at": timezone.now()},
            )
            if not first_tracked_visit:
                # Serialize concurrent page loads so an older request can never
                # overwrite a newer checkpoint and show activity twice later.
                visit = EventVisit.objects.select_for_update().get(pk=visit.pk)
                recent_changes = _recent_event_changes(
                    event, visit.last_visited_at, user=request.user
                )
                recent_change_count = sum(len(group["items"]) for group in recent_changes)
                visit.last_visited_at = timezone.now()
                visit.save(update_fields=["last_visited_at"])
    attending_user_ids = {a.user_id for a in event.attendances.all()}
    attendee_count = event.attendances.count() + BroughtPerson.objects.filter(attendance__event=event).count()
    event.attendee_count = attendee_count
    potluck_items = _potluck_items(event, attending_user_ids, attendee_count, request.user)
    translations.attach_translations(event.games.all())
    for game in event.games.all():
        for claim in game.bring_claims.all():
            claim.user_is_attending = event.planning_enabled or claim.user_id in attending_user_ids
        game.active_bring_claims = [
            claim for claim in game.bring_claims.all() if claim.user_is_attending
        ]
        for claim in game.explain_claims.all():
            claim.user_is_attending = event.planning_enabled or claim.user_id in attending_user_ids
        game.active_explain_claims = [
            claim for claim in game.explain_claims.all() if claim.user_is_attending
        ]
        game.explained_by_me = request.user.is_authenticated and any(
            claim.user_id == request.user.pk for claim in game.explain_claims.all()
        )
        game.my_explain_claim_active = game.explained_by_me and any(
            claim.user_id == request.user.pk for claim in game.active_explain_claims
        )
        game.active_bring_claim_count = len(game.active_bring_claims)
        game.claimed_by_me = request.user.is_authenticated and any(
            claim.user_id == request.user.pk for claim in game.bring_claims.all()
        )
        game.interested_by_me = request.user.is_authenticated and any(
            interest.user_id == request.user.pk for interest in game.interested_users.all()
        )
        game.needs_bringer = game.active_bring_claim_count == 0
    event.open_game_wishes = [game for game in event.games.all() if game.needs_bringer]
    for slot in event.slots.all():
        for signup in slot.signups.all():
            signup.user_is_attending = signup.user_id in attending_user_ids
        slot.signed_up_by_me = request.user.is_authenticated and any(
            signup.user_id == request.user.pk for signup in slot.signups.all()
        )
    for duty in event.duties.all():
        for signup in duty.signups.all():
            signup.user_is_attending = signup.user_id in attending_user_ids
    for day in event.days.all():
        members, guests = [], []
        for a in event.attendances.all():
            if any(d.pk == day.pk for d in a.days.all()):
                (members if a.role == "member" else guests).append(
                    {
                        "name": a.display_name,
                        "user": a.user,
                        "via": a.invited_by if a.role == "guest" else "",
                        "comment": a.comment,
                    }
                )
            for p in a.brought_people.all():
                if any(d.pk == day.pk for d in p.days.all()):
                    (members if p.role == "member" else guests).append(
                        {
                            "name": p.name,
                            "user": None,
                            "via": a.display_name if p.role == "guest" else "",
                            "comment": "",
                        }
                    )
        day.member_entries = members
        day.guest_entries = guests
        day.member_count = len(members)
        day.guest_count = len(guests)
        day.total_count = day.member_count + day.guest_count
    ideas = (
        event.ideas.select_related("author__profile")
        .prefetch_related("comments__author__profile", "likes")
        .annotate(like_count=Count("likes"))
        .order_by("-like_count", "-created_at")
    )
    for idea in ideas:
        idea.liked_by_me = request.user.is_authenticated and any(
            like.user_id == request.user.pk for like in idea.likes.all()
        )
    questions = (
        event.questions.select_related("author__profile")
        .prefetch_related(
            "likes",
            "reactions",
            "answers__author__profile",
            "answers__likes",
            "answers__reactions",
            "answers__replies__author__profile",
            "answers__replies__likes",
            "answers__replies__reactions",
        )
        .order_by("-created_at")
    )
    for question in questions:
        _tally_reactions(question, request.user)
        question.top_answers = [a for a in question.answers.all() if a.parent_id is None]
        for answer in question.top_answers:
            _tally_reactions(answer, request.user)
            for reply in answer.replies.all():
                _tally_reactions(reply, request.user)
    my_attendance = None
    if request.user.is_authenticated:
        my_attendance = event.attendances.filter(user=request.user).first()
    context = {
        "event": event,
        "today": timezone.localdate(),
        "questions": questions,
        "reaction_emoji": [e for e, _ in REACTION_EMOJI_CHOICES],
        "ideas": ideas,
        "potluck_items": potluck_items,
        "planning_groups": [
            (category, label, sorted(event.planning_items.filter(category=category).prefetch_related("completed_by__profile"), key=lambda item: ({"open": 0, "progress": 1, "waiting": 2, "done": 3}[item.planning_status], item.due_date or date.max, item.pk)))
            for category, label in PlanningItem._meta.get_field("category").choices if category != "members"
        ],
        "todo_groups": todo_groups(event) if event.planning_enabled else [],
        "duty_groups": _duty_groups(event, request.user),
        "can_manage": event.can_manage(request.user),
        "may_write": event.can_manage(request.user) or not event.is_readonly,
        "my_attendance": my_attendance,
        "attendee_count": attendee_count,
        "event_user_profiles": _event_user_profiles(event),
        "recent_changes": recent_changes,
        "recent_change_count": recent_change_count,
        "recent_changes_limited": recent_change_count >= MAX_RECENT_EVENT_CHANGES,
        "first_tracked_visit": first_tracked_visit,
    }
    identities = list(event.advert_identities.all())
    context["advert_identities"] = identities
    context["advert_highscore"] = highscore(next((items for category, label, items in context["planning_groups"] if category == "advert"), []), identities)
    advert_items = next((items for category, label, items in context["planning_groups"] if category == "advert"), [])
    context["advert_everyone_items"] = [item for item in advert_items if item.everyone]
    context["advert_status_groups"] = [
        (label, [item for item in advert_items if not item.everyone and item.status in statuses])
        for label, statuses in [("Offen", {"open"}), ("In Arbeit", {"progress", "waiting"}), ("Fertig", {"done"})]
    ]

    for category, label, items in context["planning_groups"]:
        for item in items:
            item.can_edit_status = _can_edit_planning_status(event, item, request.user)
            item.can_edit_people = _can_edit_planning_people(event, item, request.user)
            guest_prompt = not request.user.is_authenticated and not event.is_readonly and not item.everyone
            item.can_open_status = item.can_edit_status or guest_prompt
            item.can_open_people = item.can_edit_people or guest_prompt
    return render(request, "events/event_detail.html", context)


@require_POST
def event_access_code_request(request, pk):
    """Anmeldecode-Anfrage am Zugang einer passwortgeschützten Veranstaltung.

    Bekannte Personen - wer an einer bereits beendeten Veranstaltung
    teilgenommen hat (siehe is_known_person) - erhalten denselben
    Anmeldecode wie beim normalen Login und kommen nach der Code-Eingabe
    über die Bestätigungsseite von allauth ohne Zugangspasswort hinein
    (siehe has_event_access). Für alle anderen Adressen wird kein Code
    verschickt und kein Konto angelegt; die Antwort ist dennoch identisch,
    damit sich Teilnehmende nicht aufzählen lassen. Das Formular übernimmt
    allauths Rate-Limit für Code-Anfragen (je E-Mail und IP)."""
    event = get_object_or_404(Event, pk=pk)
    if not (event.has_access_password and event.allow_known_persons_signup):
        return redirect("events:event_detail", pk=event.pk)
    form = EventAccessLoginCodeForm(request.POST)
    if not form.is_valid():
        context = {"event": event, "code_form": form}
        return render(request, "events/event_password.html", context)
    user = getattr(form, "_user", None)
    if user is not None:
        initiate_login_code(request, user, form.cleaned_data["email"])
    else:
        # Kein Versand - aber derselbe Text wie nach einem tatsächlichen
        # Versand (account/messages/login_code_sent.txt), damit sich
        # bekannte und nicht bekannte Adressen nicht unterscheiden lassen.
        get_adapter().add_message(
            request,
            messages.SUCCESS,
            "account/messages/login_code_sent.txt",
            message_context={"recipient": form.cleaned_data["email"]},
        )
    confirm_url = reverse("account_confirm_login_code")
    next_url = reverse("events:event_detail", args=[event.pk])
    return redirect(f"{confirm_url}?next={quote(next_url)}")


@login_required
@require_POST
def set_display_name(request, pk):
    event = get_object_or_404(Event, pk=pk)
    # Der Anzeigename ist Kontodaten des Benutzers, kein Veranstaltungsinhalt:
    # Die Freischaltung der Veranstaltung wird geprüft, der Schreibschutz
    # beendeter Veranstaltungen hier aber bewusst nicht.
    if not has_event_access(request, event):
        raise PermissionDenied
    name = request.POST.get("display_name", "").strip()
    profile, _ = safe_get_or_create(UserProfile, user=request.user)
    profile.display_name = name
    profile.save()
    messages.success(request, "Anzeigename gespeichert.")
    return redirect("events:event_detail", pk=event.pk)


def _event_detail_anchor(event_id, anchor):
    """Return to the relevant list entry instead of the page header."""
    url = reverse("events:event_detail", args=[event_id])
    return redirect(f"{url}#{anchor}")


@login_required
def event_create(request):
    if request.method == "POST":
        form = EventForm(request.POST, request.FILES)
        formset = EventDayFormSet(request.POST, instance=Event())
        if request.POST.get("planning_enabled"):
            formset.min_num = 0
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                event = form.save(commit=False)
                event.created_by = request.user
                event.save()
                form.save_m2m()
                formset.instance = event
                formset.save()
                form.save_attachments()
            messages.success(request, "Veranstaltung angelegt.")
            return redirect("events:event_detail", pk=event.pk)
    else:
        form = EventForm()
        formset = EventDayFormSet(instance=Event())
    return render(request, "events/event_form.html", {"form": form, "formset": formset})


@login_required
def event_edit(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if not event.can_manage(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = EventForm(request.POST, request.FILES, instance=event)
        formset = EventDayFormSet(request.POST, instance=event)
        if request.POST.get("planning_enabled"):
            formset.min_num = 0
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                had_days = event.days.exists()
                form.save()
                formset.save()
                form.save_attachments()
                if event.planning_enabled and not had_days and event.days.count() == 1:
                    event.duties.filter(day__isnull=True).update(day=event.days.first())
            messages.success(request, "Veranstaltung aktualisiert.")
            return redirect("events:event_detail", pk=event.pk)
    else:
        form = EventForm(instance=event)
        formset = EventDayFormSet(instance=event)
        if event.planning_enabled:
            formset.min_num = 0
    return render(request, "events/event_form.html", {"form": form, "formset": formset, "event": event})


@login_required
def event_orga_edit(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if not event.can_manage(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = EventOrgaForm(request.POST, instance=event)
        if form.is_valid():
            form.save()
            messages.success(request, "Orga aktualisiert.")
            return redirect("events:event_detail", pk=event.pk)
    else:
        form = EventOrgaForm(instance=event)
    return render(request, "events/event_orga_form.html", {"form": form, "event": event})


@login_required
@require_POST
def event_delete(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if not event.can_manage(request.user):
        raise PermissionDenied
    title = event.title
    event.delete()
    messages.success(request, f"Veranstaltung „{title}“ wurde gelöscht.")
    return redirect("events:event_list")


@login_required
def day_attendance_export(request, day_id):
    day = get_object_or_404(EventDay, pk=day_id)
    if not day.event.can_manage(request.user):
        raise PermissionDenied

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = str(day.date)[:31]
    sheet.append(["Name", "Status", "Eingeladen durch / mitgebracht von", "Kommentar zur Anmeldung"])
    for attendance in day.attendances.select_related("user__profile").order_by("name"):
        sheet.append(
            [
                _excel_safe_text(attendance.display_name),
                _excel_safe_text(attendance.get_role_display()),
                _excel_safe_text(attendance.invited_by),
                _excel_safe_text(attendance.comment),
            ]
        )
    # Absichtlich über ALLE Anmeldungen der Veranstaltung, nicht nur die für
    # diesen Tag: eine mitgebrachte Person kann an einem anderen Tag dabei
    # sein als die Person, die sie eingetragen hat (z.B. "ich bin nur
    # freitags da, mein Kind kommt samstags mit einer Freundin").
    for attendance in day.event.attendances.select_related("user__profile").prefetch_related("brought_people__days"):
        for person in attendance.brought_people.all():
            if day in person.days.all():
                sheet.append(
                    [
                        _excel_safe_text(person.name),
                        _excel_safe_text(person.get_role_display()),
                        _excel_safe_text(f"mitgebracht von {attendance.display_name}"),
                        _excel_safe_text(attendance.comment),
                    ]
                )

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="teilnehmer_{day.event_id}_{day.date}.xlsx"'
    workbook.save(response)
    return response


def _excel_safe_text(value):
    """Force user-controlled spreadsheet cells to remain plain text."""
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return f"'{text}"
    return text


@login_required
def attendance_rsvp(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        with transaction.atomic():
            Event.objects.select_for_update().get(pk=pk)
            attendance = event.attendances.filter(user=request.user).first()
            instance = attendance or Attendance(event=event, user=request.user)
            form = AttendanceForm(request.POST, instance=instance, event=event)
            formset = BroughtPersonFormSet(request.POST, instance=instance, form_kwargs={"event": event})
            # Validate both objects even when the main form is invalid.  Using a
            # short-circuiting ``and`` here left the formset unvalidated and
            # therefore hid useful errors from people adding guests.
            form_valid = form.is_valid()
            formset_valid = formset.is_valid()
            if form_valid and formset_valid:
                attendance = form.save(commit=False)
                attendance.event = event
                attendance.user = request.user
                attendance.save()
                form.save_m2m()
                formset.instance = attendance
                formset.save()
                messages.success(request, "Deine Anmeldung wurde gespeichert.")
                return _event_detail_anchor(event.pk, "attendance-section")
            messages.error(
                request,
                "Die Anmeldung wurde nicht gespeichert. Bitte prüfe die markierten Angaben.",
            )
    else:
        attendance = event.attendances.filter(user=request.user).first()
        instance = attendance or Attendance(event=event, user=request.user)
        form = AttendanceForm(instance=instance, event=event)
        formset = BroughtPersonFormSet(instance=instance, form_kwargs={"event": event})
    return render(
        request,
        "events/attendance_form.html",
        {"event": event, "form": form, "formset": formset, "attendance": attendance},
    )


@login_required
def attendance_edit(request, attendance_id):
    attendance = get_object_or_404(Attendance, pk=attendance_id)
    event = attendance.event
    _require_event_access(request, event)
    is_owner = attendance.user_id == request.user.pk
    if not is_owner and not event.can_manage(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = AttendanceForm(request.POST, instance=attendance, event=event)
        formset = BroughtPersonFormSet(request.POST, instance=attendance, form_kwargs={"event": event})
        form_valid = form.is_valid()
        formset_valid = formset.is_valid()
        if form_valid and formset_valid:
            # Keep the account attendance and all brought-person changes as one
            # unit. If either save fails, none of the edits may remain behind.
            with transaction.atomic():
                attendance = form.save()
                formset.save()
                if not is_owner:
                    _log_orga_action(request, event, f"Anmeldung von {get_display_name(attendance.user)} bearbeitet")
            if is_owner:
                messages.success(request, "Deine Anmeldung wurde gespeichert.")
            else:
                messages.success(request, "Anmeldung als Orga gespeichert.")
            return _event_detail_anchor(event.pk, "attendance-section")
        messages.error(
            request,
            "Die Änderungen wurden nicht gespeichert. Bitte prüfe die markierten Angaben.",
        )
    else:
        form = AttendanceForm(instance=attendance, event=event)
        formset = BroughtPersonFormSet(instance=attendance, form_kwargs={"event": event})
    return render(
        request,
        "events/attendance_form.html",
        {"event": event, "form": form, "formset": formset, "attendance": attendance, "editing_other": not is_owner},
    )


@login_required
@require_POST
def attendance_cancel(request, attendance_id):
    attendance = get_object_or_404(Attendance, pk=attendance_id)
    _require_event_access(request, attendance.event)
    is_owner = attendance.user_id == request.user.pk
    if not is_owner and not attendance.event.can_manage(request.user):
        raise PermissionDenied
    event = attendance.event
    attendee = attendance.user
    event_pk = event.pk
    with transaction.atomic():
        attendance.delete()
        removed = _cleanup_attendee_commitments(event, attendee)
        if not is_owner:
            _log_orga_action(request, event, f"Anmeldung von {get_display_name(attendee)} gelöscht")
    if is_owner:
        messages.success(request, "Anmeldung zurückgezogen.")
    else:
        messages.success(request, "Anmeldung als Orga gelöscht.")
    if removed:
        messages.info(
            request,
            f"{removed} Zusage(n) von {get_display_name(attendee)} (Mitbringliste, Spiele, Dienste oder Workshops) "
            "wurden dadurch ebenfalls entfernt.",
        )
    return redirect("events:event_detail", pk=event_pk)


@login_required
@require_POST
def brought_person_cancel(request, brought_person_id):
    person = get_object_or_404(BroughtPerson, pk=brought_person_id)
    attendance = person.attendance
    _require_event_access(request, attendance.event)
    is_owner = attendance.user_id == request.user.pk
    if not is_owner and not attendance.event.can_manage(request.user):
        raise PermissionDenied
    event = attendance.event
    event_pk = event.pk
    name = person.name
    with transaction.atomic():
        person.delete()
        if not is_owner:
            _log_orga_action(
                request, event, f"Mitgebrachte Person „{name}“ (von {get_display_name(attendance.user)}) gelöscht"
            )
    if is_owner:
        messages.success(request, "Mitgebrachte Person entfernt.")
    else:
        messages.success(request, "Mitgebrachte Person als Orga gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
def decline_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        with transaction.atomic():
            Event.objects.select_for_update().get(pk=pk)
            attendance = event.attendances.filter(user=request.user).first()
            existing = event.declines.filter(user=request.user).first()
            form = DeclineForm(request.POST, instance=existing)
            if form.is_valid():
                decline = form.save(commit=False)
                decline.event = event
                decline.user = request.user
                decline.save()
                removed = 0
                if attendance:
                    if request.POST.get("delete_attendance"):
                        attendance.delete()
                        removed = _cleanup_attendee_commitments(event, request.user)
                    elif request.POST.get("delete_brought_people"):
                        attendance.brought_people.all().delete()
                messages.success(request, "Absage gespeichert.")
                if removed:
                    messages.info(
                        request,
                        f"{removed} Zusage(n) (Mitbringliste, Spiele, Dienste oder Workshops) wurden dadurch ebenfalls entfernt.",
                    )
                return _event_detail_anchor(event.pk, "attendance-section")
    else:
        attendance = event.attendances.filter(user=request.user).first()
        existing = event.declines.filter(user=request.user).first()
        form = DeclineForm(instance=existing)
    return render(
        request,
        "events/decline_form.html",
        {"event": event, "form": form, "attendance": attendance},
    )


@login_required
@require_POST
def decline_cancel(request, decline_id):
    decline = get_object_or_404(Decline, pk=decline_id)
    _require_event_access(request, decline.event)
    is_owner = decline.user_id == request.user.pk
    if not is_owner and not decline.event.can_manage(request.user):
        raise PermissionDenied
    event = decline.event
    declined_user = decline.user
    event_pk = event.pk
    decline.delete()
    if is_owner:
        messages.success(request, "Absage zurückgezogen.")
    else:
        _log_orga_action(request, event, f"Absage von {get_display_name(declined_user)} gelöscht")
        messages.success(request, "Absage als Orga gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
def potluck_item_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        form = PotluckItemForm(request.POST)
        if form.is_valid():
            item = form.save(commit=False)
            item.event = event
            item.created_by = request.user
            item.save()
            messages.success(request, "Eintrag zur Mitbringliste hinzugefügt.")
            return _event_detail_anchor(event.pk, f"potluck-{item.pk}")
    else:
        form = PotluckItemForm()
    return render(request, "events/potluck_item_form.html", {"event": event, "form": form})


@login_required
def potluck_item_edit(request, item_id):
    item = get_object_or_404(PotluckItem, pk=item_id)
    _require_event_access(request, item.event)
    is_owner = item.created_by_id == request.user.pk and not item.claims.exists()
    if not is_owner and not item.event.can_manage(request.user):
        raise PermissionDenied
    if request.method == "POST":
        original_title = item.title
        form = PotluckItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            if is_owner:
                messages.success(request, "Eintrag aktualisiert.")
            else:
                owner = get_display_name(item.created_by) if item.created_by else "unbekannt"
                _log_orga_action(
                    request,
                    item.event,
                    f'Mitbring-Eintrag „{original_title}“ von {owner} bearbeitet',
                )
                messages.success(request, "Eintrag als Orga aktualisiert.")
            return _event_detail_anchor(item.event_id, f"potluck-{item.pk}")
    else:
        form = PotluckItemForm(instance=item)
    return render(request, "events/potluck_item_form.html", {"event": item.event, "form": form, "item": item})


@login_required
@require_POST
def potluck_item_delete(request, item_id):
    item = get_object_or_404(PotluckItem, pk=item_id)
    _require_event_access(request, item.event)
    is_owner = item.created_by_id == request.user.pk and not item.claims.exists()
    if not is_owner and not item.event.can_manage(request.user):
        raise PermissionDenied
    event = item.event
    event_pk = item.event_id
    title = item.title
    owner = get_display_name(item.created_by) if item.created_by else "unbekannt"
    item.delete()
    if is_owner:
        messages.success(request, "Eintrag gelöscht.")
    else:
        _log_orga_action(request, event, f'Mitbring-Eintrag „{title}“ von {owner} gelöscht')
        messages.success(request, "Eintrag als Orga gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
def potluck_claim_create(request, item_id):
    item = get_object_or_404(PotluckItem, pk=item_id)
    _require_event_access(request, item.event)
    if item.is_variable:
        item.attendee_count = (
            item.event.attendances.count()
            + BroughtPerson.objects.filter(attendance__event=item.event).count()
        )
    if request.method == "POST":
        form = PotluckClaimForm(request.POST)
        if form.is_valid():
            claim = form.save(commit=False)
            claim.item = item
            claim.user = request.user
            claim.save()
            messages.success(request, "Danke, dein Beitrag wurde eingetragen.")
            return _event_detail_anchor(item.event_id, f"potluck-{item.pk}")
    else:
        form = PotluckClaimForm()
    return render(request, "events/potluck_claim_form.html", {"item": item, "form": form})


def _potluck_people(event):
    """Personen, für die die Orga eine Mitbring-Zusage eintragen kann: die
    Angemeldeten; bei Veranstaltungen mit Vorab-Orga (ohne Anmeldeliste)
    zusätzlich alle, die die Veranstaltung schon besucht haben, und die Orga."""
    users = {a.user for a in event.attendances.select_related("user__profile")}
    if event.planning_enabled:
        users.update(v.user for v in event.visits.select_related("user__profile"))
        users.update(event.editors.select_related("profile"))
        if event.created_by:
            users.add(event.created_by)
    return [u for u in users if u.is_active]


@login_required
def potluck_claim_create_for_other(request, item_id):
    item = get_object_or_404(PotluckItem, pk=item_id)
    event = item.event
    _require_event_access(request, event)
    if not event.can_manage(request.user):
        raise PermissionDenied
    if item.is_variable:
        item.attendee_count = (
            event.attendances.count() + BroughtPerson.objects.filter(attendance__event=event).count()
        )
    people = _potluck_people(event)
    if request.method == "POST":
        form = PotluckOrgaClaimForm(request.POST, people=people)
        if form.is_valid():
            claim = form.save(commit=False)
            claim.item = item
            claim.entered_by = request.user
            claim.save()
            _log_orga_action(
                request,
                event,
                f'Mitbring-Zusage für {claim.person_label} eingetragen: '
                f'{item.format_quantity(claim.quantity)} × „{item.title}“',
            )
            messages.success(request, f"Zusage für {claim.person_label} eingetragen.")
            return _event_detail_anchor(event.pk, f"potluck-{item.pk}")
    else:
        form = PotluckOrgaClaimForm(people=people)
    guest_names = sorted(
        set(BroughtPerson.objects.filter(attendance__event=event).values_list("name", flat=True)),
        key=str.lower,
    )
    return render(
        request,
        "events/potluck_claim_form.html",
        {"event": event, "item": item, "form": form, "for_other": True, "guest_names": guest_names},
    )


@login_required
def potluck_claim_edit(request, claim_id):
    # Anders als beim Zurückziehen (nur Person selbst oder Admin) darf hier
    # auch die Orga die Menge korrigieren - das Ändern ist weniger heikel
    # als das komplette Löschen einer fremden Zusage.
    claim = get_object_or_404(PotluckClaim, pk=claim_id)
    _require_event_access(request, claim.item.event)
    is_owner = claim.user_id == request.user.pk
    if not is_owner and not claim.item.event.can_manage(request.user):
        raise PermissionDenied
    if claim.item.is_variable:
        claim.item.attendee_count = (
            claim.item.event.attendances.count()
            + BroughtPerson.objects.filter(attendance__event=claim.item.event).count()
        )
    if request.method == "POST":
        form = PotluckClaimForm(request.POST, instance=claim)
        if form.is_valid():
            form.save()
            if is_owner:
                messages.success(request, "Menge aktualisiert.")
            else:
                owner = claim.person_label
                _log_orga_action(
                    request,
                    claim.item.event,
                    f'Menge von {owner} bei „{claim.item.title}“ geändert',
                )
                messages.success(request, "Menge als Orga aktualisiert.")
            return redirect("events:event_detail", pk=claim.item.event_id)
    else:
        form = PotluckClaimForm(instance=claim)
    return render(
        request,
        "events/potluck_claim_form.html",
        {"event": claim.item.event, "item": claim.item, "form": form, "claim": claim},
    )


@login_required
@require_POST
def potluck_claim_cancel(request, claim_id):
    # Die Mitbringliste selbst ist kollaborativ (siehe potluck_item_edit),
    # eine Zusage zurückziehen darf aber nur, wer sie abgegeben hat, oder ein
    # Admin - nicht die Orga, damit niemand fremde Zusagen einfach löscht.
    # Ausnahme: Zusagen, die die Orga selbst stellvertretend eingetragen hat,
    # darf sie auch wieder löschen.
    claim = get_object_or_404(PotluckClaim, pk=claim_id)
    event = claim.item.event
    _require_event_access(request, event)
    is_owner = claim.user_id is not None and claim.user_id == request.user.pk
    entered_by_orga = claim.entered_by_id is not None and event.can_manage(request.user)
    if not (is_owner or request.user.is_superuser or entered_by_orga):
        raise PermissionDenied
    claim.delete()
    if is_owner:
        messages.success(request, "Claim zurückgezogen.")
    else:
        _log_orga_action(request, event, f'Mitbring-Zusage von {claim.person_label} bei „{claim.item.title}“ gelöscht')
        messages.success(request, "Zusage als Orga gelöscht.")
    return redirect("events:event_detail", pk=event.pk)


@login_required
def idea_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        form = IdeaForm(request.POST)
        if form.is_valid():
            idea = form.save(commit=False)
            idea.event = event
            idea.author = request.user
            idea.save()
            messages.success(request, "Idee hinzugefügt.")
            return _event_detail_anchor(event.pk, f"idea-{idea.pk}")
    else:
        form = IdeaForm()
    return render(request, "events/idea_form.html", {"event": event, "form": form})


def idea_permalink(request, idea_id):
    """Resolve a stable, shareable idea URL, preserving password access flow."""
    idea = get_object_or_404(Idea.objects.select_related("event"), pk=idea_id)
    if not has_event_access(request, idea.event):
        return event_detail(request, idea.event_id)
    return _event_detail_anchor(idea.event_id, f"idea-{idea.pk}")


@login_required
@require_POST
def idea_like_toggle(request, idea_id):
    with transaction.atomic():
        idea = get_object_or_404(Idea.objects.select_for_update(), pk=idea_id)
        _require_event_access(request, idea.event)
        like = IdeaLike.objects.filter(idea=idea, user=request.user).first()
        if like:
            like.delete()
        else:
            IdeaLike.objects.create(idea=idea, user=request.user)
    url = reverse("events:event_detail", args=[idea.event_id])
    return redirect(f"{url}#idea-{idea.pk}")


@login_required
@require_POST
def idea_comment_create(request, idea_id):
    idea = get_object_or_404(Idea, pk=idea_id)
    _require_event_access(request, idea.event)
    form = IdeaCommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.idea = idea
        comment.author = request.user
        with transaction.atomic():
            comment.save()
            queue_idea_comment_notifications(comment)
    url = reverse("events:event_detail", args=[idea.event_id])
    return redirect(f"{url}#idea-{idea.pk}")


@login_required
@require_POST
def idea_delete(request, idea_id):
    idea = get_object_or_404(Idea, pk=idea_id)
    _require_event_access(request, idea.event)
    if idea.author_id != request.user.pk and not idea.event.can_manage(request.user):
        raise PermissionDenied
    event_pk = idea.event_id
    idea.delete()
    messages.success(request, "Idee gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
def question_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        form = QuestionForm(request.POST)
        if form.is_valid():
            question = form.save(commit=False)
            question.event = event
            question.author = request.user
            question.save()
            messages.success(request, "Frage gestellt.")
            return _event_detail_anchor(event.pk, f"question-{question.pk}")
    else:
        form = QuestionForm()
    return render(request, "events/question_form.html", {"event": event, "form": form})


@login_required
@require_POST
def question_delete(request, question_id):
    question = get_object_or_404(Question, pk=question_id)
    _require_event_access(request, question.event)
    if question.author_id != request.user.pk and not question.event.can_manage(request.user):
        raise PermissionDenied
    event_pk = question.event_id
    question.delete()
    messages.success(request, "Frage gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
@require_POST
def question_update(request, question_id):
    question = get_object_or_404(Question, pk=question_id)
    _require_event_access(request, question.event)
    if question.author_id != request.user.pk and not question.event.can_manage(request.user):
        raise PermissionDenied
    form = QuestionForm(request.POST, instance=question)
    if form.is_valid():
        form.save()
        messages.success(request, "Frage aktualisiert.")
    url = reverse("events:event_detail", args=[question.event_id])
    return redirect(f"{url}#question-{question.pk}")


@login_required
@require_POST
def answer_create(request, question_id):
    question = get_object_or_404(Question, pk=question_id)
    _require_event_access(request, question.event)
    form = AnswerForm(request.POST)
    if form.is_valid():
        answer = form.save(commit=False)
        answer.question = question
        answer.author = request.user
        answer.save()
    url = reverse("events:event_detail", args=[question.event_id])
    return redirect(f"{url}#question-{question.pk}")


@login_required
@require_POST
def answer_delete(request, answer_id):
    answer = get_object_or_404(Answer, pk=answer_id)
    _require_event_access(request, answer.question.event)
    if answer.author_id != request.user.pk and not answer.question.event.can_manage(request.user):
        raise PermissionDenied
    event_pk = answer.question.event_id
    question_pk = answer.question_id
    answer.delete()
    messages.success(request, "Antwort gelöscht.")
    url = reverse("events:event_detail", args=[event_pk])
    return redirect(f"{url}#question-{question_pk}")


@login_required
@require_POST
def answer_update(request, answer_id):
    answer = get_object_or_404(Answer, pk=answer_id)
    _require_event_access(request, answer.question.event)
    if answer.author_id != request.user.pk and not answer.question.event.can_manage(request.user):
        raise PermissionDenied
    form = AnswerForm(request.POST, instance=answer)
    if form.is_valid():
        form.save()
        messages.success(request, "Antwort aktualisiert.")
    url = reverse("events:event_detail", args=[answer.question.event_id])
    return redirect(f"{url}#question-{answer.question_id}")


@login_required
@require_POST
def answer_reply_create(request, answer_id):
    target = get_object_or_404(Answer, pk=answer_id)
    _require_event_access(request, target.question.event)
    form = AnswerForm(request.POST)
    if form.is_valid():
        reply = form.save(commit=False)
        reply.question_id = target.question_id
        reply.parent = target.parent or target
        reply.author = request.user
        reply.save()
    url = reverse("events:event_detail", args=[target.question.event_id])
    return redirect(f"{url}#question-{target.question_id}")


@login_required
@require_POST
def question_like_toggle(request, question_id):
    with transaction.atomic():
        question = get_object_or_404(Question.objects.select_for_update(), pk=question_id)
        _require_event_access(request, question.event)
        like = QuestionLike.objects.filter(question=question, user=request.user).first()
        if like:
            like.delete()
        else:
            QuestionLike.objects.create(question=question, user=request.user)
    url = reverse("events:event_detail", args=[question.event_id])
    return redirect(f"{url}#question-{question.pk}")


@login_required
@require_POST
def answer_like_toggle(request, answer_id):
    with transaction.atomic():
        answer = get_object_or_404(Answer.objects.select_for_update(), pk=answer_id)
        _require_event_access(request, answer.question.event)
        like = AnswerLike.objects.filter(answer=answer, user=request.user).first()
        if like:
            like.delete()
        else:
            AnswerLike.objects.create(answer=answer, user=request.user)
    url = reverse("events:event_detail", args=[answer.question.event_id])
    return redirect(f"{url}#question-{answer.question_id}")


@login_required
@require_POST
def question_reaction_toggle(request, question_id):
    emoji = request.POST.get("emoji", "")
    if emoji not in dict(REACTION_EMOJI_CHOICES):
        raise Http404
    with transaction.atomic():
        question = get_object_or_404(Question.objects.select_for_update(), pk=question_id)
        _require_event_access(request, question.event)
        reaction = QuestionReaction.objects.filter(question=question, user=request.user, emoji=emoji).first()
        if reaction:
            reaction.delete()
        else:
            QuestionReaction.objects.create(question=question, user=request.user, emoji=emoji)
    url = reverse("events:event_detail", args=[question.event_id])
    return redirect(f"{url}#question-{question.pk}")


@login_required
@require_POST
def answer_reaction_toggle(request, answer_id):
    emoji = request.POST.get("emoji", "")
    if emoji not in dict(REACTION_EMOJI_CHOICES):
        raise Http404
    with transaction.atomic():
        answer = get_object_or_404(Answer.objects.select_for_update(), pk=answer_id)
        _require_event_access(request, answer.question.event)
        reaction = AnswerReaction.objects.filter(answer=answer, user=request.user, emoji=emoji).first()
        if reaction:
            reaction.delete()
        else:
            AnswerReaction.objects.create(answer=answer, user=request.user, emoji=emoji)
    url = reverse("events:event_detail", args=[answer.question.event_id])
    return redirect(f"{url}#question-{answer.question_id}")


@login_required
def slot_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        form = SlotForm(request.POST, event=event)
        if form.is_valid():
            slot = form.save(commit=False)
            slot.event = event
            slot.created_by = request.user
            slot.save()
            messages.success(request, "Angebot/Workshop angelegt.")
            return _event_detail_anchor(event.pk, f"slot-{slot.pk}")
    else:
        form = SlotForm(event=event)
    return render(request, "events/slot_form.html", {"event": event, "form": form})


@login_required
def slot_edit(request, slot_id):
    slot = get_object_or_404(Slot, pk=slot_id)
    _require_event_access(request, slot.event)
    if slot.created_by_id != request.user.pk and not slot.event.can_manage(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = SlotForm(request.POST, instance=slot, event=slot.event)
        if form.is_valid():
            form.save()
            messages.success(request, "Angebot/Workshop aktualisiert.")
            return _event_detail_anchor(slot.event_id, f"slot-{slot.pk}")
    else:
        form = SlotForm(instance=slot, event=slot.event)
    return render(request, "events/slot_form.html", {"event": slot.event, "form": form, "slot": slot})


@login_required
@require_POST
def slot_delete(request, slot_id):
    slot = get_object_or_404(Slot, pk=slot_id)
    _require_event_access(request, slot.event)
    if slot.created_by_id != request.user.pk and not slot.event.can_manage(request.user):
        raise PermissionDenied
    event_pk = slot.event_id
    slot.delete()
    messages.success(request, "Angebot/Workshop samt Anmeldungen und Kommentaren gelöscht.")
    return _event_detail_anchor(event_pk, "slots-section")


@login_required
@require_POST
def slot_signup(request, slot_id):
    with transaction.atomic():
        slot = get_object_or_404(Slot.objects.select_for_update(), pk=slot_id)
        _require_event_access(request, slot.event)
        if slot.signups.filter(user=request.user).exists():
            messages.info(request, "Du bist bereits angemeldet.")
        elif slot.is_full and not slot.allow_waitlist:
            messages.error(request, "Dieses Angebot ist bereits voll.")
        else:
            was_full = slot.is_full
            SlotSignup.objects.create(slot=slot, user=request.user)
            if was_full:
                messages.success(request, "Du stehst auf der Warteliste.")
            else:
                messages.success(request, "Anmeldung erfolgreich.")
    return _event_detail_anchor(slot.event_id, f"slot-{slot.pk}")


@login_required
@require_POST
def slot_signup_cancel(request, signup_id):
    signup = get_object_or_404(SlotSignup, pk=signup_id)
    _require_event_access(request, signup.slot.event)
    is_owner = signup.user_id == request.user.pk
    if not is_owner and not signup.slot.event.can_manage(request.user):
        raise PermissionDenied
    event = signup.slot.event
    signup_user = signup.user
    event_pk = event.pk
    slot_title = signup.slot.title
    signup.delete()
    if is_owner:
        messages.success(request, "Anmeldung zurückgezogen.")
    else:
        _log_orga_action(
            request, event, f"Angebot/Workshop-Anmeldung von {get_display_name(signup_user)} für „{slot_title}“ gelöscht"
        )
        messages.success(request, "Anmeldung als Orga gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
@require_POST
def slot_comment_create(request, slot_id):
    slot = get_object_or_404(Slot, pk=slot_id)
    _require_event_access(request, slot.event)
    form = SlotCommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.slot = slot
        comment.author = request.user
        comment.save()
    return _event_detail_anchor(slot.event_id, f"slot-{slot.pk}")


@login_required
def game_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        form = GameEntryForm(request.POST, event=event)
        if form.is_valid():
            game = form.save(commit=False)
            game.event = event
            game.created_by = request.user
            game.save()
            if game.type != "wish":
                GameBringClaim.objects.get_or_create(game=game, user=request.user)
            if form.cleaned_data.get("can_explain"):
                GameExplainClaim.objects.get_or_create(game=game, user=request.user)
            messages.success(request, "Spiel hinzugefügt.")
            if form.bgg_sync_error:
                messages.warning(request, form.bgg_sync_error)
            return _event_detail_anchor(event.pk, f"game-{game.pk}")
    else:
        form = GameEntryForm(event=event)
    return render(request, "events/game_form.html", {"event": event, "form": form})


@login_required
def game_bulk_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        form = GameBulkForm(request.POST, event=event)
        if form.is_valid():
            game_type = form.cleaned_data["type"]
            created, joined, already = [], [], []
            with transaction.atomic():
                for entry in form.cleaned_data["entries"]:
                    if entry["existing_pk"]:
                        game = GameEntry.objects.select_for_update().get(pk=entry["existing_pk"])
                        changes = []
                        if game_type != "wish":
                            _claim, was_new = GameBringClaim.objects.get_or_create(game=game, user=request.user)
                            if was_new:
                                changes.append("Mitbringer")
                        if entry["explain"]:
                            _claim, was_new = GameExplainClaim.objects.get_or_create(game=game, user=request.user)
                            if was_new:
                                changes.append("Erklärbär")
                        if changes:
                            joined.append(f"{game.name} ({' und '.join(changes)})")
                        else:
                            already.append(game.name)
                        continue
                    game = GameEntry.objects.create(
                        event=event,
                        name=entry["name"],
                        type=game_type,
                        bgg_id=entry["bgg_id"],
                        created_by=request.user,
                    )
                    if game_type != "wish":
                        GameBringClaim.objects.create(game=game, user=request.user)
                    if entry["explain"]:
                        GameExplainClaim.objects.create(game=game, user=request.user)
                    created.append(game)
            bgg_error = _sync_new_bgg_games([game for game in created if game.bgg_id])
            if created:
                messages.success(request, f"{len(created)} {'Spiel' if len(created) == 1 else 'Spiele'} hinzugefügt.")
            if joined:
                messages.success(request, "Schon auf der Liste, du bist dort jetzt eingetragen: " + ", ".join(joined) + ".")
            if already:
                messages.info(request, "Schon auf der Liste, nichts geändert: " + ", ".join(already) + ".")
            if bgg_error:
                messages.warning(request, f"BGG-Daten konnten noch nicht geladen werden ({bgg_error}). Sie werden automatisch nachgeholt.")
            return _event_detail_anchor(event.pk, "games-section")
    else:
        form = GameBulkForm(event=event)
    return render(request, "events/game_bulk_form.html", {
        "event": event,
        "form": form,
        "existing_names": list(event.games.values_list("name", flat=True)),
    })


def _sync_new_bgg_games(games):
    """Lädt die BGG-Daten neu angelegter Spiele gebündelt nach. Schlägt das
    fehl, bleibt bgg_synced_at leer und sync_bgg_games holt es später nach."""
    error = ""
    for start in range(0, len(games), bgg.MAX_IDS_PER_REQUEST):
        chunk = games[start:start + bgg.MAX_IDS_PER_REQUEST]
        try:
            data_by_id = bgg.fetch_bgg_games([game.bgg_id for game in chunk])
        except (bgg.BggLookupError, ValueError) as exc:
            error = str(exc)
            continue
        synced_at = timezone.now()
        for game in chunk:
            data = data_by_id.get(game.bgg_id)
            if not data:
                continue
            game.min_players = data["min_players"]
            game.max_players = data["max_players"]
            game.playtime_minutes = data["playtime_minutes"]
            game.min_age = data["min_age"]
            game.bgg_year = data["year"]
            game.complexity = data["complexity"]
            game.bgg_rating = data["rating"]
            game.bgg_description = data["description"]
            game.bgg_categories = data["categories"]
            game.bgg_mechanics = data["mechanics"]
            game.thumbnail_url = data["thumbnail_url"]
            game.bgg_synced_at = synced_at
            game.save()
    return error


@login_required
def game_edit(request, game_id):
    game = get_object_or_404(GameEntry, pk=game_id)
    _require_event_access(request, game.event)
    if game.created_by_id != request.user.pk and not game.event.can_manage(request.user):
        raise PermissionDenied
    if request.method == "POST":
        was_wish = game.type == "wish"
        form = GameEntryForm(request.POST, instance=game, event=game.event)
        if form.is_valid():
            form.save()
            # Nur beim tatsächlichen Wechsel weg von "Wunsch" automatisch eine
            # Zusage anlegen (für die Person, die gerade bearbeitet) - sonst
            # würde jede unabhängige Änderung (z.B. Beschreibung) eine bereits
            # zurückgezogene Zusage stillschweigend wiederherstellen.
            if was_wish and game.type != "wish":
                GameBringClaim.objects.get_or_create(game=game, user=request.user)
            messages.success(request, "Spiel aktualisiert.")
            if form.bgg_sync_error:
                messages.warning(request, form.bgg_sync_error)
            return redirect("events:event_detail", pk=game.event_id)
    else:
        form = GameEntryForm(instance=game, event=game.event)
    return render(request, "events/game_form.html", {"event": game.event, "form": form, "game": game})


@login_required
@transaction.non_atomic_requests
def game_translation(request, game_id):
    game = get_object_or_404(GameEntry.objects.select_related("event"), pk=game_id)
    _require_event_access(request, game.event)
    if game.created_by_id != request.user.pk and not game.event.can_manage(request.user):
        raise PermissionDenied
    if not game.bgg_id:
        messages.error(request, "Bitte zuerst eine BGG-ID mit dem Spiel verknüpfen.")
        return redirect("events:game_edit", game_id=game.pk)
    translation = BggTranslation.objects.filter(pk=game.bgg_id).first()
    form = BggTranslationForm(initial={
        "bgg_id": game.bgg_id,
        "revision": translation.revision if translation else 0,
        "text_de": translation.text_de if translation else "",
    })
    if request.method == "POST":
        if request.POST.get("bgg_id") != str(game.bgg_id):
            messages.error(request, "Die BGG-Verknüpfung wurde inzwischen geändert. Bitte prüfe das Spiel erneut.")
            return redirect("events:game_translation", game_id=game.pk)
        action = request.POST.get("action")
        if action == "translate":
            try:
                translation = translations.request_translation(game, request.user)
            except translations.TranslationError as exc:
                messages.error(request, str(exc))
            else:
                if translation.text_de:
                    messages.success(request, "Die deutsche Beschreibung ist gespeichert und wird in allen Veranstaltungen verwendet.")
            return redirect("events:game_translation", game_id=game.pk)
        if action != "save":
            return HttpResponse(status=400)
        form = BggTranslationForm(request.POST)
        if form.is_valid():
            try:
                translations.save_translation(game, request.user, form.cleaned_data["text_de"], form.cleaned_data["revision"])
            except translations.TranslationError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Deutsche Beschreibung für alle Veranstaltungen gespeichert.")
                return _event_detail_anchor(game.event_id, f"game-{game.pk}")
    return render(request, "events/game_translation.html", {
        "game": game, "event": game.event, "translation": translation, "form": form,
        "translation_configured": bool(settings.DEEPL_API_KEY),
        "source_text": translation.source_text if translation and translation.source_text else bgg.description_text(game.bgg_description),
    })


@login_required
@require_POST
def game_bgg_sync(request, game_id):
    game = get_object_or_404(GameEntry, pk=game_id)
    _require_event_access(request, game.event)
    if game.created_by_id != request.user.pk and not game.event.can_manage(request.user):
        raise PermissionDenied
    if not game.bgg_id:
        messages.error(request, "Bitte zuerst eine BGG-ID eintragen.")
    else:
        game.bgg_synced_at = None
        game.save(update_fields=["bgg_synced_at"])
        messages.success(request, "BGG-Aktualisierung wurde für den Hintergrundjob vorgemerkt.")
    return _event_detail_anchor(game.event_id, f"game-{game.pk}")


@login_required
def game_bgg_search(request):
    """Liefert Titel-Treffer von BGG als JSON, damit das Formular eine ID raten kann."""
    query = (request.GET.get("q") or "").strip()
    try:
        results = bgg.search_bgg_games(query)
    except bgg.BggLookupError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"results": results})


@login_required
def game_bgg_lookup(request):
    """Liefert die Spieldaten einer BGG-ID als JSON, damit das Formular sie vorbefüllen kann."""
    raw_id = (request.GET.get("id") or "").strip()
    if not raw_id.isdigit():
        return JsonResponse({"error": "Bitte eine gültige BGG-ID (Zahl) angeben."}, status=400)
    try:
        data = bgg.fetch_bgg_game(int(raw_id))
    except (bgg.BggLookupError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    data["bgg_id"] = int(raw_id)
    return JsonResponse(data)


@login_required
@require_POST
def game_interest_toggle(request, game_id):
    with transaction.atomic():
        game = get_object_or_404(GameEntry.objects.select_for_update(), pk=game_id)
        _require_event_access(request, game.event)
        interest = GameInterest.objects.filter(game=game, user=request.user).first()
        if interest:
            interest.delete()
        else:
            GameInterest.objects.create(game=game, user=request.user)
    url = reverse("events:event_detail", args=[game.event_id])
    return redirect(f"{url}#game-{game.pk}")


@login_required
@require_POST
def game_bring_claim_create(request, game_id):
    with transaction.atomic():
        game = get_object_or_404(GameEntry.objects.select_for_update(), pk=game_id)
        _require_event_access(request, game.event)
        GameBringClaim.objects.get_or_create(game=game, user=request.user)
    messages.success(request, "Danke, du bringst das Spiel mit.")
    url = reverse("events:event_detail", args=[game.event_id])
    return redirect(f"{url}#game-{game.pk}")


@login_required
@require_POST
def game_bring_claim_cancel(request, claim_id):
    # Nur der Autor der Zusage oder ein Admin darf sie zurückziehen (nicht die
    # Orga), analog zu potluck_claim_cancel.
    claim = get_object_or_404(GameBringClaim, pk=claim_id)
    _require_event_access(request, claim.game.event)
    if claim.user_id != request.user.pk and not request.user.is_superuser:
        raise PermissionDenied
    game = claim.game
    claim.delete()
    messages.success(request, "Zusage zurückgezogen.")
    url = reverse("events:event_detail", args=[game.event_id])
    return redirect(f"{url}#game-{game.pk}")


@login_required
@require_POST
def game_explain_claim_create(request, game_id):
    with transaction.atomic():
        game = get_object_or_404(GameEntry.objects.select_for_update(), pk=game_id)
        _require_event_access(request, game.event)
        GameExplainClaim.objects.get_or_create(game=game, user=request.user)
    messages.success(request, "Danke, du erklärst das Spiel.")
    url = reverse("events:event_detail", args=[game.event_id])
    return redirect(f"{url}#game-{game.pk}")


@login_required
@require_POST
def game_explain_claim_set(request, game_id):
    """Eigene Erklär-Zusage per Klick auf das 🐻-Icon setzen (explain=1) oder
    entfernen (explain=0). Zielzustand statt Umschalten, damit Doppelklicks
    und überholende Hintergrund-Anfragen idempotent bleiben. Antwortet mit
    JSON samt frisch gerendertem Popup und Detailbereich."""
    wanted = request.POST.get("explain") == "1"
    with transaction.atomic():
        game = get_object_or_404(GameEntry.objects.select_for_update().select_related("event"), pk=game_id)
        event = game.event
        _require_event_access(request, event)
        if wanted:
            GameExplainClaim.objects.get_or_create(game=game, user=request.user)
        else:
            GameExplainClaim.objects.filter(game=game, user=request.user).delete()
    if request.headers.get("Accept") != "application/json":
        messages.success(request, "Danke, du erklärst das Spiel." if wanted else "Zusage zurückgezogen.")
        return _event_detail_anchor(event.pk, f"game-{game.pk}")
    attending_user_ids = set(event.attendances.values_list("user_id", flat=True))
    claims = list(game.explain_claims.select_related("user__profile"))
    for claim in claims:
        claim.user_is_attending = event.planning_enabled or claim.user_id in attending_user_ids
    game.active_explain_claims = [claim for claim in claims if claim.user_is_attending]
    game.explained_by_me = any(claim.user_id == request.user.pk for claim in claims)
    game.my_explain_claim_active = any(claim.user_id == request.user.pk for claim in game.active_explain_claims)
    mine = game.explained_by_me or game.bring_claims.filter(user=request.user).exists()
    context = {"game": game, "event": event, "may_write": True, "user": request.user}
    # explain_claims.all() im Detailbereich soll die bereits annotierten Claims nutzen.
    game._prefetched_objects_cache = {"explain_claims": claims}
    return JsonResponse({
        "explaining": game.explained_by_me,
        "count": len(game.active_explain_claims),
        "mine": mine,
        "popup_html": render_to_string("events/_game_explain_popup_content.html", context, request=request),
        "section_html": render_to_string("events/_game_explain_section.html", context, request=request),
    })


@login_required
@require_POST
def game_explain_claim_cancel(request, claim_id):
    # Nur der Autor der Zusage oder ein Admin darf sie zurückziehen (nicht die
    # Orga), analog zu potluck_claim_cancel.
    claim = get_object_or_404(GameExplainClaim, pk=claim_id)
    _require_event_access(request, claim.game.event)
    if claim.user_id != request.user.pk and not request.user.is_superuser:
        raise PermissionDenied
    game = claim.game
    claim.delete()
    messages.success(request, "Zusage zurückgezogen.")
    url = reverse("events:event_detail", args=[game.event_id])
    return redirect(f"{url}#game-{game.pk}")


@login_required
@require_POST
def game_comment_create(request, game_id):
    game = get_object_or_404(GameEntry, pk=game_id)
    _require_event_access(request, game.event)
    form = GameCommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.game = game
        comment.author = request.user
        comment.save()
    return _event_detail_anchor(game.event_id, f"game-{game.pk}")


@login_required
@require_POST
def game_comment_edit(request, comment_id):
    comment = get_object_or_404(GameComment, pk=comment_id)
    _require_event_access(request, comment.game.event)
    if comment.author_id != request.user.pk and not comment.game.event.can_manage(request.user):
        raise PermissionDenied
    form = GameCommentForm(request.POST, instance=comment)
    if form.is_valid():
        form.save()
        messages.success(request, "Kommentar aktualisiert.")
    game = comment.game
    url = reverse("events:event_detail", args=[game.event_id])
    return redirect(f"{url}#game-{game.pk}")


@login_required
@require_POST
def game_comment_delete(request, comment_id):
    comment = get_object_or_404(GameComment, pk=comment_id)
    _require_event_access(request, comment.game.event)
    if comment.author_id != request.user.pk and not comment.game.event.can_manage(request.user):
        raise PermissionDenied
    game = comment.game
    comment.delete()
    messages.success(request, "Kommentar gelöscht.")
    url = reverse("events:event_detail", args=[game.event_id])
    return redirect(f"{url}#game-{game.pk}")


@login_required
@require_POST
def game_delete(request, game_id):
    game = get_object_or_404(GameEntry, pk=game_id)
    _require_event_access(request, game.event)
    if game.created_by_id != request.user.pk and not game.event.can_manage(request.user):
        raise PermissionDenied
    event_pk = game.event_id
    game.delete()
    messages.success(request, "Spiel entfernt.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
def duty_create(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if request.method == "POST":
        form = DutyForm(request.POST, event=event)
        if form.is_valid():
            duty = form.save(commit=False)
            duty.event = event
            duty.created_by = request.user
            duty.save()
            messages.success(request, "Dienst angelegt.")
            return _event_detail_anchor(event.pk, f"duty-{duty.pk}")
    else:
        form = DutyForm(event=event)
    return render(request, "events/duty_form.html", {"event": event, "form": form})


@login_required
def duty_edit(request, duty_id):
    duty = get_object_or_404(Duty, pk=duty_id)
    _require_event_access(request, duty.event)
    if duty.created_by_id != request.user.pk and not duty.event.can_manage(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = DutyForm(request.POST, instance=duty, event=duty.event)
        if form.is_valid():
            form.save()
            messages.success(request, "Dienst aktualisiert.")
            return redirect("events:event_detail", pk=duty.event_id)
    else:
        form = DutyForm(instance=duty, event=duty.event)
    return render(request, "events/duty_form.html", {"event": duty.event, "form": form, "duty": duty})


@login_required
@require_POST
def duty_delete(request, duty_id):
    duty = get_object_or_404(Duty, pk=duty_id)
    _require_event_access(request, duty.event)
    if duty.created_by_id != request.user.pk and not duty.event.can_manage(request.user):
        raise PermissionDenied
    event_pk = duty.event_id
    duty.delete()
    messages.success(request, "Dienst gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


@login_required
@require_POST
def duty_signup(request, duty_id):
    with transaction.atomic():
        duty = get_object_or_404(Duty.objects.select_for_update(), pk=duty_id)
        _require_event_access(request, duty.event)
        # Springer gibt es nur im Dienstplan; Auf- und Abbau werden regulär besetzt.
        is_reserve = request.POST.get("role") == "reserve" and duty.category == "service"
        if duty.signups.filter(user=request.user).exists():
            messages.info(request, "Du bist bereits eingetragen.")
        elif duty.is_full and not is_reserve:
            messages.error(request, "Für diesen Dienst werden keine weiteren Personen mehr benötigt.")
        else:
            DutySignup.objects.create(duty=duty, user=request.user, is_reserve=is_reserve)
            messages.success(request, "Eintragung erfolgreich.")
    return _event_detail_anchor(duty.event_id, f"duty-{duty.pk}")


@login_required
@require_POST
def duty_signup_cancel(request, signup_id):
    signup = get_object_or_404(DutySignup, pk=signup_id)
    _require_event_access(request, signup.duty.event)
    is_owner = signup.user_id == request.user.pk
    if not is_owner and not signup.duty.event.can_manage(request.user):
        raise PermissionDenied
    event = signup.duty.event
    signup_user = signup.user
    event_pk = event.pk
    duty_title = signup.duty.title
    signup.delete()
    if is_owner:
        messages.success(request, "Eintragung zurückgezogen.")
    else:
        _log_orga_action(
            request, event, f"Dienst-Eintragung von {get_display_name(signup_user)} für „{duty_title}“ gelöscht"
        )
        messages.success(request, "Eintragung als Orga gelöscht.")
    return redirect("events:event_detail", pk=event_pk)


PLANNING_CONFLICT = "Dieser Eintrag wurde inzwischen geändert. Bitte lade den aktuellen Stand und gleiche deine Eingaben damit ab."


def _planning_revision_matches(request, item):
    return request.POST.get("revision") == str(item.revision)


def _planning_conflict_response(request, event, item):
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"error": PLANNING_CONFLICT}, status=409)
    messages.error(request, PLANNING_CONFLICT)
    return _event_detail_anchor(event.pk, f"planning-{item.pk}")


def _log_planning_changes(request, event, item, previous=None):
    prefix = f'{item.get_category_display()}: „{item.title[:150]}“'
    if previous is None:
        _log_orga_action(request, event, f"{prefix} angelegt")
        return
    for field in PlanningItemForm._meta.fields:
        old, new = previous.get(field), getattr(item, field)
        if old == new:
            continue
        model_field = PlanningItem._meta.get_field(field)
        choices = dict(model_field.choices or [])
        old = choices.get(old, old)
        new = choices.get(new, new)
        _log_orga_action(request, event,
                         f"{prefix}: {model_field.verbose_name}: {_change_text(old or '–', 110)} → {_change_text(new or '–', 110)}")


@login_required
@transaction.atomic
def planning_item_edit(request, pk, item_id=None):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if not event.can_manage(request.user):
        raise PermissionDenied
    item = get_object_or_404(PlanningItem.objects.select_for_update(), pk=item_id, event=event) if item_id else None
    previous = {field: getattr(item, field) for field in PlanningItemForm._meta.fields} if item else None
    category = item.category if item else request.GET.get("category", request.POST.get("category", "orga"))
    if category not in PlanningItemForm.PRESENTATION:
        category = "orga"
    initial = {"todo_period": request.GET.get("period", "")} if not item else {}
    form = PlanningItemForm(request.POST if request.method == "POST" else None, instance=item, initial=initial, category=category, show_status=event.responsibilities_show_status)
    conflict = False
    if request.method == "POST":
        valid = form.is_valid()
        if item and not _planning_revision_matches(request, item):
            form.add_error(None, PLANNING_CONFLICT)
            conflict = True
        elif valid:
            item = form.save(commit=False)
            item.event = event
            if not item.pk:
                item.created_by = request.user
            else:
                item.revision += 1
            item.save()
            _log_planning_changes(request, event, item, previous)
            return _event_detail_anchor(event.pk, f"planning-{item.pk}")
    name = form.presentation["name"]
    return render(request, "events/planning_item_form.html", {
        "event": event, "form": form, "conflict": conflict,
        "heading": f"{name} bearbeiten" if item else f"{name} anlegen",
        "save_label": f"{name} speichern", "category": category,
    }, status=409 if conflict else 200)


def _can_edit_planning_status(event, item, user):
    if item.everyone:
        return False
    if event.can_manage(user):
        return True
    return (user.is_authenticated and not event.is_readonly
        and item.category in ("advert", "orga")
        and (item.created_by_id == user.pk or item.has_responsible_user(user)))


def _can_edit_planning_people(event, item, user):
    return (_can_edit_planning_status(event, item, user)
        or (not item.everyone and not item.people.strip() and user.is_authenticated
            and not event.is_readonly and item.category in ("advert", "orga")))


def _planning_field_edit(request, pk, item_id, field):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    item = get_object_or_404(PlanningItem.objects.select_for_update(), pk=item_id, event=event)
    allowed = _can_edit_planning_status if field == "status" else _can_edit_planning_people
    if not any(row["key"] == item.category for row in event.visible_lists) or not allowed(event, item, request.user):
        raise PermissionDenied
    form_class = PlanningStatusForm if field == "status" else PlanningPeopleForm
    form = form_class(request.POST if request.method == "POST" else None, instance=item)
    conflict = False
    previous = {name: getattr(item, name) for name in PlanningItemForm._meta.fields}
    popup = request.headers.get("X-Planning-Popup") == "1"
    if request.method == "POST" and form.is_valid():
        if not _planning_revision_matches(request, item):
            form.add_error(None, PLANNING_CONFLICT)
            conflict = True
        else:
            item = form.save(commit=False)
            item.revision += 1
            item.save(update_fields=[field, "revision"])
            _log_planning_changes(request, event, item, previous)
            response = _event_detail_anchor(event.pk, f"planning-{item.pk}")
            return JsonResponse({"location": response.url}) if popup else response
    return render(request, "events/_planning_field_form.html" if popup else "events/planning_status_form.html", {
        "event": event, "item": item, "form": form,
        "heading": "Status bearbeiten" if field == "status" else "Verantwortliche bearbeiten",
        "save_label": "Status speichern" if field == "status" else "Verantwortliche speichern",
    }, status=409 if conflict else (400 if request.method == "POST" and popup else 200))


@login_required
@transaction.atomic
def planning_status_edit(request, pk, item_id):
    return _planning_field_edit(request, pk, item_id, "status")


@login_required
@transaction.atomic
def planning_people_edit(request, pk, item_id):
    return _planning_field_edit(request, pk, item_id, "people")


@login_required
@transaction.atomic
def advert_report(request, pk, item_id=None):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if not any(row["key"] == "advert" for row in event.visible_lists):
        raise PermissionDenied
    item = get_object_or_404(PlanningItem.objects.select_for_update(), pk=item_id,
        event=event, category="advert", everyone=False) if item_id else None
    form = AdvertReportForm(request.POST if request.method == "POST" else None,
        item=item, initial={"published_on": timezone.localdate()})
    conflict = False
    if request.method == "POST" and form.is_valid():
        if item and not _planning_revision_matches(request, item):
            form.add_error(None, PLANNING_CONFLICT)
            conflict = True
        elif not item and event.planning_items.filter(category="advert", title__iexact=form.cleaned_data["title"]).exists():
            form.add_error("title", "Dieser Ort / diese Aktion ist schon eingetragen. Bitte melde die Werbung beim bestehenden Eintrag.")
        else:
            previous = {field: getattr(item, field) for field in PlanningItemForm._meta.fields} if item else None
            if not item:
                item = PlanningItem(event=event, category="advert", title=form.cleaned_data["title"], created_by=request.user)
            actor_name = get_display_name(request.user).strip()
            names = [actor_name] if form.cleaned_data["include_self"] else []
            names += split_names(form.cleaned_data["collaborators"])
            existing = [name.casefold() for name in split_names(item.people)]
            people = item.people
            for name in names:
                if name and name.casefold() not in existing:
                    people = f"{people.rstrip()}, {name}" if people.strip() else name
                    existing.append(name.casefold())
            if len(people) > PlanningItem._meta.get_field("people").max_length:
                form.add_error("collaborators", "Die Namensliste ist zu lang. Bitte wende dich an die Orga.")
            else:
                item.people = people
                if item_id or form.cleaned_data["done"]:
                    item.status = "done"
                    item.published_on = form.cleaned_data["published_on"] or timezone.localdate()
                note = form.cleaned_data["notes"]
                if note:
                    dated_note = f"{timezone.localdate():%d.%m.%Y} – {actor_name}: {note}"
                    item.notes = f"{item.notes.rstrip()}\n{dated_note}" if item.notes.strip() else dated_note
                if item.pk:
                    item.revision += 1
                item.save()
                _log_planning_changes(request, event, item, previous)
                messages.success(request, "Deine Werbung wurde eingetragen.")
                return _event_detail_anchor(event.pk, f"planning-{item.pk}")
    return render(request, "events/advert_report_form.html", {
        "event": event, "form": form, "item": item,
    }, status=409 if conflict else 200)


@login_required
@require_POST
@transaction.atomic
def planning_item_assign_self(request, pk, item_id):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    item = get_object_or_404(PlanningItem.objects.select_for_update(), pk=item_id, event=event, category__in=["advert", "orga"], everyone=False)
    if not item.has_responsible_user(request.user):
        name = get_display_name(request.user).strip()
        people = f"{item.people.rstrip()}, {name}" if item.people.strip() else name
        if not name or len(people) > PlanningItem._meta.get_field("people").max_length:
            messages.error(request, "Dein Name konnte nicht ergänzt werden. Bitte wende dich an die Orga.")
            return _event_detail_anchor(event.pk, f"planning-{item.pk}")
        previous = {field: getattr(item, field) for field in PlanningItemForm._meta.fields}
        item.people = people
        item.revision += 1
        item.save(update_fields=["people", "revision"])
        _log_planning_changes(request, event, item, previous)
    return _event_detail_anchor(event.pk, f"planning-{item.pk}")


@login_required
@require_POST
@transaction.atomic
def planning_item_participate(request, pk, item_id):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    item = get_object_or_404(PlanningItem.objects.select_for_update(), pk=item_id, event=event, category="advert", everyone=True)
    participated = item.completed_by.filter(pk=request.user.pk).exists()
    remove = request.POST.get("remove") == "on"
    if remove and participated:
        item.completed_by.remove(request.user)
    elif not remove and not participated:
        item.completed_by.add(request.user)
    else:
        return _event_detail_anchor(event.pk, f"planning-{item.pk}")
    action = "zurückgenommen" if remove else "bestätigt"
    _log_orga_action(request, event, f'Werbung: „{item.title[:150]}“: Teilnahme von {get_display_name(request.user)} {action}')
    return _event_detail_anchor(event.pk, f"planning-{item.pk}")


@login_required
@require_POST
@transaction.atomic
def planning_item_complete(request, pk, item_id):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if not event.can_manage(request.user):
        raise PermissionDenied
    item = get_object_or_404(PlanningItem.objects.select_for_update(), pk=item_id, event=event, category="members")
    if not _planning_revision_matches(request, item):
        return _planning_conflict_response(request, event, item)
    previous = {field: getattr(item, field) for field in PlanningItemForm._meta.fields}
    item.status = "done" if request.POST.get("done") == "on" else "open"
    item.revision += 1
    item.save(update_fields=["status", "revision"])
    _log_planning_changes(request, event, item, previous)
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"status": item.status, "label": item.get_status_display(), "revision": item.revision})
    return _event_detail_anchor(event.pk, f"planning-{item.pk}")


@login_required
@require_POST
@transaction.atomic
def planning_item_delete(request, pk, item_id):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if not event.can_manage(request.user):
        raise PermissionDenied
    item = get_object_or_404(PlanningItem.objects.select_for_update(), pk=item_id, event=event)
    if not _planning_revision_matches(request, item):
        return _planning_conflict_response(request, event, item)
    category = item.category
    _log_orga_action(request, event, f'{item.get_category_display()}: „{item.title}“ gelöscht (Status: {item.get_status_display()}, verantwortlich: {_change_text(item.people or "–", 150)})')
    item.delete()
    return _event_detail_anchor(event.pk, f"{category}-section")


@login_required
def planning_history(request, pk):
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if not event.can_manage(request.user):
        raise PermissionDenied
    page = Paginator(event.orga_action_logs.select_related("actor__profile").order_by("-created_at", "-pk"), 50).get_page(request.GET.get("page"))
    return render(request, "events/planning_history.html", {"event": event, "page": page})


@login_required
@transaction.atomic
def planning_import(request, pk):
    event = get_object_or_404(Event.objects.select_for_update(), pk=pk)
    _require_event_access(request, event)
    if not event.can_manage(request.user) or not event.planning_enabled:
        raise PermissionDenied
    sources = Event.objects.filter(planning_enabled=True).exclude(pk=event.pk)
    if not request.user.is_superuser:
        sources = sources.filter(Q(created_by=request.user) | Q(editors=request.user)).distinct()
    source_id = request.POST.get("source") if request.method == "POST" else request.GET.get("source")
    source = None
    items = []
    if source_id:
        if not source_id.isdigit():
            return HttpResponse(status=400)
        source = get_object_or_404(sources, pk=source_id)
        items = list(source.planning_items.select_for_update())
    if request.method == "POST":
        if source is None:
            return HttpResponse(status=400)
        source_day = source.days.order_by("date").first()
        target_day = event.days.order_by("date").first()
        offset = (target_day.date - source_day.date) if source_day and target_day else None
        copied = 0
        for original in items:
            if event.planning_items.filter(Q(template_source=original) | Q(category=original.category, title=original.title)).exists():
                continue
            values = {field: getattr(original, field) for field in PlanningItemForm._meta.fields}
            values["last_done_by"] = original.people or original.last_done_by
            if request.POST.get("reset_people") == "on":
                values["people"] = ""
            if request.POST.get("reset_status") == "on":
                values["status"] = "open"
            values["due_date"] = original.due_date + offset if original.due_date and offset is not None else None
            values["published_on"] = None
            values["publication_url"] = ""
            item = PlanningItem.objects.create(event=event, template_source=original, created_by=request.user, **values)
            _log_orga_action(request, event, f'{item.get_category_display()}: „{item.title[:150]}“ aus „{source.title[:150]}“ übernommen')
            copied += 1
        messages.success(request, f"{copied} Aufgaben übernommen; {len(items) - copied} bereits vorhandene Einträge übersprungen.")
        return _event_detail_anchor(event.pk, "orga-section")
    return render(request, "events/planning_import.html", {"event": event, "sources": sources, "source": source, "items": items})


@login_required
def duty_schedule_create(request, pk):
    from .planning import create_duty_grid
    event = get_object_or_404(Event, pk=pk)
    _require_event_access(request, event)
    if not event.can_manage(request.user):
        raise PermissionDenied
    form = DutyScheduleForm(request.POST if request.method == "POST" else None, event=event)
    if request.method == "POST" and form.is_valid():
        create_duty_grid(event=event, created_by=request.user, **form.cleaned_data)
        return _event_detail_anchor(event.pk, "duties-section")
    return render(request, "events/planning_form.html", {"event": event, "form": form, "heading": "Dienstplan-Raster anlegen"})


def list_attachment_file(request, pk, attachment_id):
    attachment = get_object_or_404(ListAttachment.objects.select_related("event"), pk=attachment_id, event_id=pk)
    if not has_event_access(request, attachment.event):
        raise PermissionDenied
    if not attachment.event.can_manage(request.user):
        if attachment.list_key not in {row["key"] for row in attachment.event.visible_lists}:
            raise Http404
    try:
        file = attachment.file.open("rb")
    except FileNotFoundError:
        raise Http404
    response = FileResponse(file, content_type=attachment.content_type,
        as_attachment=not attachment.is_image or request.GET.get("download") == "1",
        filename=attachment.original_name)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@login_required
@transaction.atomic
def advert_aliases(request, pk, identity_id=None):
    from .models import AdvertIdentity
    from .forms import AdvertIdentityForm
    event = get_object_or_404(Event.objects.select_for_update(), pk=pk)
    _require_event_access(request, event)
    if not event.can_manage(request.user) or not any(row["key"] == "advert" for row in event.visible_lists):
        raise PermissionDenied
    identity = get_object_or_404(AdvertIdentity, event=event, pk=identity_id) if identity_id else AdvertIdentity(event=event)
    form = AdvertIdentityForm(request.POST if request.method == "POST" else None, instance=identity)
    if request.method == "POST":
        if request.POST.get("delete") and identity.pk:
            identity.delete()
            return _event_detail_anchor(event.pk, "advert-section")
        if form.is_valid():
            form.save()
            messages.success(request, "Die Namenszuordnung wurde gespeichert.")
            return _event_detail_anchor(event.pk, "advert-section")
    return render(request, "events/advert_aliases.html", {"event": event, "form": form, "identity": identity})
