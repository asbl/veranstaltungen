"""Shared BGG translations with an at-most-once DeepL request per BGG ID and
source revision.

The reservation is committed BEFORE contacting DeepL. Even a timeout or worker
crash cannot trigger another paid request for the same source. Such records
can be repaired manually. Do not wrap request_translation or refresh_translation
in an outer transaction or add HTTP retries.

An existing translation is refreshed only when its BGG source text changed and
nobody edited the German text manually (edited_at stays authoritative).
"""
import json

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import bgg
from .models import BggTranslation, GameEntry


class TranslationError(Exception):
    pass


def attach_translations(games):
    """Resolve all event games in one query, including newly linked BGG IDs."""
    translations = BggTranslation.objects.in_bulk({g.bgg_id for g in games if g.bgg_id})
    for game in games:
        game.bgg_translation = translations.get(game.bgg_id)


def _payload_for(source):
    payload = {"text": [source], "target_lang": "DE", "preserve_formatting": True}
    if len(json.dumps(payload).encode("utf-8")) > 120_000:
        raise TranslationError("Diese Beschreibung ist für eine einzelne Übersetzung zu lang. Bitte manuell eintragen.")
    return payload


def _post_translation(key, payload):
    """One DeepL request. Raises TranslationError with a presentable reason and
    is never retried - callers reserve the attempt around this call."""
    base_url = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    try:
        response = requests.post(
            base_url + "/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            json=payload,
            timeout=(5, 25),
            allow_redirects=False,
        )
        if response.status_code != 200:
            reason = {
                403: "DeepL hat den API-Schlüssel abgelehnt.",
                429: "DeepL begrenzt zurzeit die Anfragen.",
                456: "Das kostenlose DeepL-Kontingent ist ausgeschöpft.",
            }.get(response.status_code, "DeepL konnte die Beschreibung nicht übersetzen.")
            raise TranslationError(reason)
        text = response.json()["translations"][0]["text"]
        if not isinstance(text, str) or not text.strip():
            raise TranslationError("DeepL hat keine Übersetzung zurückgegeben.")
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, TranslationError) as exc:
        raise TranslationError(
            str(exc) if isinstance(exc, TranslationError) else "Von DeepL konnte keine gültige Antwort empfangen werden."
        ) from None
    return text.strip()


def request_translation(game, user):
    if not game.bgg_id:
        raise TranslationError("Bitte zuerst eine BGG-ID mit dem Spiel verknüpfen.")
    existing = BggTranslation.objects.filter(pk=game.bgg_id).first()
    if existing:
        return existing
    key = settings.DEEPL_API_KEY
    if not key:
        raise TranslationError("Die automatische Übersetzung ist noch nicht eingerichtet.")
    source = game.bgg_description or GameEntry.objects.filter(
        bgg_id=game.bgg_id
    ).exclude(bgg_description="").values_list("bgg_description", flat=True).first()
    if not source:
        try:
            source = bgg.fetch_bgg_game(game.bgg_id)["description"]
        except bgg.BggLookupError:
            raise TranslationError("Die BGG-Beschreibung konnte nicht geladen werden.") from None
    source = bgg.description_text(source)
    if not source:
        raise TranslationError("BGG liefert für dieses Spiel noch keine Beschreibung.")
    payload = _payload_for(source)

    # Durable prohibits a caller from accidentally rolling back the reservation
    # after DeepL has already processed (and counted) the request.
    with transaction.atomic(durable=True):
        translation, created = BggTranslation.objects.get_or_create(
            bgg_id=game.bgg_id,
            defaults={"source_text": source, "status": "pending", "attempted_at": timezone.now(), "updated_by": user},
        )
    if not created:
        return translation

    try:
        text = _post_translation(key, payload)
    except TranslationError as exc:
        BggTranslation.objects.filter(pk=game.bgg_id, status="pending", revision=0).update(
            status="failed", error_message=str(exc), revision=1,
        )
    else:
        BggTranslation.objects.filter(pk=game.bgg_id, status="pending", revision=0).update(
            status="ready", text_de=text, translated_at=timezone.now(), revision=1,
        )
    return BggTranslation.objects.get(pk=game.bgg_id)


def refresh_translation(translation, source, user):
    """Retranslate one record whose BGG source text changed. Only automatic
    translations are eligible (status ready/failed, edited_at empty) - manual
    corrections always win. The conditional UPDATE claims the attempt, so
    concurrent runs or a manual save in between cannot cause a second request
    for the same source. A failed refresh keeps the previous German text."""
    key = settings.DEEPL_API_KEY
    if not key:
        raise TranslationError("Die automatische Übersetzung ist noch nicht eingerichtet.")
    payload = _payload_for(source)
    claimed = BggTranslation.objects.filter(
        pk=translation.pk, status__in=["ready", "failed"], edited_at__isnull=True, revision=translation.revision,
    ).update(
        status="pending", source_text=source, attempted_at=timezone.now(), updated_by=user,
        error_message="", revision=translation.revision + 1,
    )
    if not claimed:
        return BggTranslation.objects.get(pk=translation.pk)
    try:
        text = _post_translation(key, payload)
    except TranslationError as exc:
        BggTranslation.objects.filter(
            pk=translation.pk, status="pending", revision=translation.revision + 1,
        ).update(status="failed", error_message=str(exc), revision=translation.revision + 2)
    else:
        BggTranslation.objects.filter(
            pk=translation.pk, status="pending", revision=translation.revision + 1,
        ).update(status="ready", text_de=text, translated_at=timezone.now(), revision=translation.revision + 2)
    return BggTranslation.objects.get(pk=translation.pk)


def refresh_changed_translations(limit=5):
    """Scheduled companion for changed BGG sources: retranslate automatic
    translations whose freshest locally cached BGG description differs from
    the stored source text, oldest attempt first. Manual edits and sources
    that did not change again are skipped; games without a cached description
    have nothing to compare and are skipped. Returns one (bgg_id, status,
    error_message) tuple per attempt, with status None for errors that
    consumed no attempt."""
    candidates = list(
        BggTranslation.objects.filter(edited_at__isnull=True, status__in=["ready", "failed"])
        .order_by("attempted_at", "pk")
    )
    if not candidates:
        return []
    freshest = {}
    rows = (GameEntry.objects.filter(bgg_id__in=[t.pk for t in candidates])
            .exclude(bgg_description="")
            .order_by("bgg_id", F("bgg_synced_at").desc(nulls_last=True), "pk")
            .values_list("bgg_id", "bgg_description"))
    for bgg_id, description in rows:
        freshest.setdefault(bgg_id, bgg.description_text(description))
    outcomes = []
    for translation in candidates:
        if len(outcomes) >= max(int(limit), 0):
            break
        source = freshest.get(translation.pk)
        if not source or source == translation.source_text:
            continue
        try:
            refreshed = refresh_translation(translation, source, None)
        except TranslationError as exc:
            outcomes.append((translation.pk, None, str(exc)))
        else:
            outcomes.append((translation.pk, refreshed.status, refreshed.error_message))
    return outcomes


def translate_missing_games(limit=5):
    """Scheduled companion to the manual translate button: request one DeepL
    translation per BGG ID that has no BggTranslation record yet. Records that
    already exist - ready, failed, pending, or manually edited - are never
    touched again, preserving the at-most-once guarantee and manual
    corrections. Games without a usable description raise TranslationError
    without consuming an attempt and are simply retried on the next scheduled
    run. Returns one (bgg_id, status, error_message) tuple per attempt, with
    status None for errors that consumed no attempt."""
    bgg_ids = list(
        GameEntry.objects.exclude(bgg_id=None)
        .exclude(bgg_id__in=BggTranslation.objects.values("pk"))
        .values_list("bgg_id", flat=True)
        .distinct()
        .order_by("bgg_id")[: max(int(limit), 0)]
    )
    outcomes = []
    for bgg_id in bgg_ids:
        # Prefer the freshest cached description so a first translation uses the
        # same text a later refresh compares against. Any entry works:
        # request_translation shares source texts and translations across all
        # entries with the same BGG ID.
        game = (GameEntry.objects.filter(bgg_id=bgg_id).exclude(bgg_description="")
                .order_by(F("bgg_synced_at").desc(nulls_last=True), "pk").first())
        if game is None:
            game = GameEntry.objects.filter(bgg_id=bgg_id).first()
        if game is None:
            continue
        try:
            translation = request_translation(game, None)
        except TranslationError as exc:
            outcomes.append((bgg_id, None, str(exc)))
        else:
            outcomes.append((bgg_id, translation.status, translation.error_message))
    return outcomes


def save_translation(game, user, text, revision):
    """Prevent stale forms and late API responses from overwriting corrections."""
    with transaction.atomic():
        translation, _ = BggTranslation.objects.get_or_create(bgg_id=game.bgg_id)
        translation = BggTranslation.objects.select_for_update().get(pk=game.bgg_id)
        if translation.is_running:
            raise TranslationError("Die Übersetzung läuft noch. Bitte lade die Seite gleich erneut.")
        if translation.revision != revision:
            raise TranslationError("Die Beschreibung wurde inzwischen geändert. Bitte öffne die aktuelle Fassung in einem neuen Tab und gleiche deine Änderungen ab.")
        translation.text_de = text
        if not translation.source_text:
            translation.source_text = bgg.description_text(game.bgg_description)
        translation.status = "ready"
        translation.edited_at = timezone.now()
        translation.updated_by = user
        translation.revision += 1
        translation.error_message = ""
        translation.save()
        return translation
