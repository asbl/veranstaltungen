import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connections
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from .models import BggTranslation, Event, GameEntry
from .translations import (
    TranslationError, attach_translations, refresh_changed_translations, request_translation, save_translation,
    translate_missing_games,
)


@override_settings(DEEPL_API_KEY="test-secret:fx")
class SharedTranslationTests(TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="translation-owner")
        self.event = Event.objects.create(title="Spieleabend", created_by=self.owner)
        self.game = GameEntry.objects.create(event=self.event, name="Catan", bgg_id=13,
                                             bgg_description="Build &mdash; trade.\nNew paragraph.", created_by=self.owner)
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.owner)
        self.url = f"/spiele/{self.game.pk}/uebersetzung/"
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"translations": [{"text": "Bauen — handeln.\nNeuer Absatz."}]}

    @patch("events.translations.requests.post")
    def test_translate_once_and_reuse_in_another_event_and_after_deletion(self, post):
        post.return_value = self.response
        first = request_translation(self.game, self.owner)
        other_event = Event.objects.create(title="Später", created_by=self.owner)
        other_game = GameEntry.objects.create(event=other_event, name="Siedler", bgg_id=13, created_by=self.owner)
        self.event.delete()
        second = request_translation(other_game, self.owner)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.text_de, "Bauen — handeln.\nNeuer Absatz.")
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api-free.deepl.com/v2/translate")
        self.assertEqual(kwargs["headers"]["Authorization"], "DeepL-Auth-Key test-secret:fx")
        self.assertEqual(kwargs["json"]["text"], ["Build — trade.\nNew paragraph."])
        self.assertEqual(kwargs["json"]["target_lang"], "DE")
        self.assertFalse(kwargs["allow_redirects"])

    @patch("events.translations.requests.post")
    def test_global_correction_display_and_print_without_more_requests(self, post):
        post.return_value = self.response
        translation = request_translation(self.game, self.owner)
        complete = "Korrigierte deutsche Beschreibung. " * 40 + "SCHLUSS"
        response = self.client.post(self.url, {"action": "save", "bgg_id": 13,
                                             "revision": translation.revision, "text_de": complete})
        self.assertEqual(response.status_code, 302)
        another = Event.objects.create(title="Neuer Abend", created_by=self.owner)
        game = GameEntry.objects.create(event=another, name="Anderer Titel", bgg_id=13,
                                       description="Kurzer eigener Text", created_by=self.owner)
        detail = self.client.get(f"/{another.pk}/")
        self.assertContains(detail, "Beschreibung (Deutsch)", count=2)
        self.assertContains(detail, "SCHLUSS")
        self.assertContains(detail, "BGG-Original anzeigen")
        printed = self.client.get(f"/{another.pk}/spiele/erklaerbaeren/drucken/")
        self.assertContains(printed, complete)
        self.assertContains(printed, "Deutsche Beschreibung (bearbeitet)")
        self.assertEqual(request_translation(game, self.owner).text_de, complete)
        post.assert_called_once()

    @patch("events.translations.requests.post")
    def test_manual_description_avoids_api_and_stale_edits_are_rejected(self, post):
        saved = save_translation(self.game, self.owner, "Von Hand übersetzt", 0)
        with self.assertRaises(TranslationError):
            save_translation(self.game, self.owner, "Veraltete Änderung", 0)
        self.assertEqual(request_translation(self.game, self.owner).text_de, saved.text_de)
        post.assert_not_called()

    @patch("events.translations.requests.post")
    def test_two_concurrent_requests_reserve_only_one_translation(self, post):
        entered = ThreadEvent()
        release = ThreadEvent()
        def response(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Parallel request did not finish")
            return self.response
        post.side_effect = response
        def translate():
            close_old_connections()
            try:
                return request_translation(GameEntry.objects.get(pk=self.game.pk), self.owner)
            finally:
                connections["default"].close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(translate)
            try:
                self.assertTrue(entered.wait(10))
                second = pool.submit(translate).result(timeout=10)
                self.assertEqual(second.status, "pending")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10).status, "ready")
        post.assert_called_once()
        self.assertEqual(BggTranslation.objects.count(), 1)

    @patch("events.translations.requests.post")
    def test_timeout_is_not_retried_and_can_be_manually_repaired(self, post):
        post.side_effect = requests.Timeout("sensitive content must not be shown")
        first = request_translation(self.game, self.owner)
        self.assertEqual(first.status, "failed")
        self.assertNotIn("sensitive", first.error_message)
        request_translation(self.game, self.owner)
        saved = save_translation(self.game, self.owner, "Manuell ergänzt", first.revision)
        self.assertEqual(saved.status, "ready")
        post.assert_called_once()

    @patch("events.translations.requests.post")
    def test_error_and_invalid_responses_are_durable(self, post):
        cases = [Mock(status_code=403), Mock(status_code=456), Mock(status_code=500),
                 Mock(status_code=200, json=Mock(return_value={"translations": []})),
                 Mock(status_code=200, json=Mock(side_effect=ValueError("invalid"))),
                 Mock(status_code=200, json=Mock(return_value={"translations": [{"text": " "}]}))]
        for index, response in enumerate(cases, start=100):
            with self.subTest(index=index):
                self.game.bgg_id = index
                post.return_value = response
                self.assertEqual(request_translation(self.game, self.owner).status, "failed")
                request_translation(self.game, self.owner)
        self.assertEqual(post.call_count, len(cases))

    @patch("events.translations.requests.post")
    def test_stale_worker_cannot_replace_manual_recovery(self, post):
        def response(*args, **kwargs):
            BggTranslation.objects.filter(pk=13).update(attempted_at=timezone.now() - datetime.timedelta(minutes=3))
            save_translation(self.game, self.owner, "Manuelle Wiederherstellung", 0)
            return self.response
        post.side_effect = response
        self.assertEqual(request_translation(self.game, self.owner).text_de, "Manuelle Wiederherstellung")
        self.assertIsNone(BggTranslation.objects.get(pk=13).translated_at)

    @patch("events.translations.requests.post")
    def test_active_translation_cannot_be_overwritten(self, post):
        BggTranslation.objects.create(bgg_id=13, status="pending", attempted_at=timezone.now())
        with self.assertRaises(TranslationError):
            save_translation(self.game, self.owner, "Zu früh", 0)
        self.assertEqual(request_translation(self.game, self.owner).status, "pending")
        post.assert_not_called()

    @override_settings(DEEPL_API_KEY="")
    @patch("events.translations.requests.post")
    def test_missing_configuration_does_not_consume_attempt(self, post):
        with self.assertRaises(TranslationError):
            request_translation(self.game, self.owner)
        self.assertFalse(BggTranslation.objects.exists())
        saved = save_translation(self.game, self.owner, "Vorhanden", 0)
        self.assertEqual(request_translation(self.game, self.owner).pk, saved.pk)
        post.assert_not_called()

    @patch("events.translations.bgg.fetch_bgg_game")
    @patch("events.translations.requests.post")
    def test_missing_description_and_oversize_do_not_consume_attempt(self, post, fetch):
        self.game.bgg_description = ""
        self.game.save()
        fetch.return_value = {"description": ""}
        with self.assertRaises(TranslationError):
            request_translation(self.game, self.owner)
        self.game.bgg_description = "x" * 130_000
        with self.assertRaises(TranslationError):
            request_translation(self.game, self.owner)
        self.assertFalse(BggTranslation.objects.exists())
        post.assert_not_called()

    @patch("events.translations.requests.post")
    def test_scheduled_run_translates_missing_ids_once_and_keeps_existing_records(self, post):
        post.return_value = self.response
        GameEntry.objects.create(event=self.event, name="Fertig", bgg_id=100, bgg_description="A.", created_by=self.owner)
        BggTranslation.objects.create(bgg_id=100, text_de="Fertige Übersetzung", status="ready")
        GameEntry.objects.create(event=self.event, name="Fehlgeschlagen", bgg_id=101, bgg_description="B.", created_by=self.owner)
        BggTranslation.objects.create(bgg_id=101, status="failed", revision=1, error_message="DeepL-Fehler")
        GameEntry.objects.create(event=self.event, name="Läuft", bgg_id=102, bgg_description="C.", created_by=self.owner)
        BggTranslation.objects.create(bgg_id=102, status="pending", attempted_at=timezone.now())
        edited_game = GameEntry.objects.create(event=self.event, name="Manuell", bgg_id=103,
                                                bgg_description="D.", created_by=self.owner)
        save_translation(edited_game, self.owner, "Von Hand gepflegt", 0)
        GameEntry.objects.create(event=self.event, name="Catan nochmal", bgg_id=13, created_by=self.owner)
        GameEntry.objects.create(event=self.event, name="Neu", bgg_id=104, bgg_description="Fresh.", created_by=self.owner)
        GameEntry.objects.create(event=self.event, name="Ohne BGG", created_by=self.owner)

        outcomes = translate_missing_games(limit=10)

        self.assertEqual([(bgg_id, status) for bgg_id, status, _ in outcomes], [(13, "ready"), (104, "ready")])
        self.assertEqual(post.call_count, 2)
        records = {record.pk: record for record in BggTranslation.objects.all()}
        self.assertEqual(records[100].text_de, "Fertige Übersetzung")
        self.assertEqual(records[101].status, "failed")
        self.assertEqual(records[102].status, "pending")
        self.assertEqual(records[103].text_de, "Von Hand gepflegt")
        self.assertTrue(records[103].edited_at)
        for bgg_id in (13, 104):
            self.assertEqual(records[bgg_id].status, "ready")
            self.assertEqual(records[bgg_id].text_de, "Bauen — handeln.\nNeuer Absatz.")
            self.assertIsNone(records[bgg_id].updated_by)

    @patch("events.translations.requests.post")
    def test_scheduled_run_respects_batch_size(self, post):
        post.return_value = self.response
        for index in range(20, 25):
            GameEntry.objects.create(event=self.event, name=f"Spiel {index}", bgg_id=index,
                                     bgg_description="Text.", created_by=self.owner)
        outcomes = translate_missing_games(limit=2)
        self.assertEqual([bgg_id for bgg_id, _, _ in outcomes], [13, 20])
        self.assertEqual(post.call_count, 2)
        translate_missing_games(limit=2)
        self.assertEqual(post.call_count, 4)
        self.assertEqual(BggTranslation.objects.count(), 4)

    @patch("events.translations.bgg.fetch_bgg_game")
    @patch("events.translations.requests.post")
    def test_scheduled_run_continues_after_errors_and_consumes_no_attempt(self, post, fetch):
        post.return_value = self.response
        GameEntry.objects.create(event=self.event, name="Ohne Beschreibung", bgg_id=200, created_by=self.owner)
        fetch.return_value = {"description": ""}
        GameEntry.objects.create(event=self.event, name="Beschreibbar", bgg_id=201,
                                 bgg_description="Translatable.", created_by=self.owner)
        outcomes = translate_missing_games(limit=10)
        self.assertEqual([(bgg_id, status) for bgg_id, status, _ in outcomes],
                         [(13, "ready"), (200, None), (201, "ready")])
        self.assertIn("keine Beschreibung", dict((bgg_id, error) for bgg_id, _, error in outcomes)[200])
    @patch("events.translations.requests.post")
    def test_scheduled_run_uses_freshest_cached_description(self, post):
        post.return_value = self.response
        GameEntry.objects.create(event=self.event, name="Alte Kopie", bgg_id=13, bgg_description="Outdated text.",
                                 bgg_synced_at=timezone.now() - datetime.timedelta(days=30), created_by=self.owner)
        self.game.bgg_description = "Fresher text."
        self.game.bgg_synced_at = timezone.now()
        self.game.save()
        translate_missing_games(limit=1)
        self.assertEqual(post.call_args.kwargs["json"]["text"], ["Fresher text."])

    @patch("events.translations.requests.post")
    def test_scheduled_refresh_retranslates_changed_source_once(self, post):
        post.return_value = self.response
        request_translation(self.game, self.owner)
        self.game.bgg_description = "Completely rewritten description."
        self.game.bgg_synced_at = timezone.now()
        self.game.save()

        outcomes = refresh_changed_translations(limit=10)

        self.assertEqual([(bgg_id, status) for bgg_id, status, _ in outcomes], [(13, "ready")])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["json"]["text"], ["Completely rewritten description."])
        record = BggTranslation.objects.get(pk=13)
        self.assertEqual(record.text_de, "Bauen — handeln.\nNeuer Absatz.")
        self.assertEqual(record.source_text, "Completely rewritten description.")
        self.assertEqual(record.revision, 3)
        self.assertIsNone(record.edited_at)
        # Unveränderte Quelle: spätere Läufe stellen keine weitere Anfrage.
        self.assertEqual(refresh_changed_translations(limit=10), [])
        self.assertEqual(post.call_count, 2)

    @patch("events.translations.requests.post")
    def test_scheduled_refresh_skips_manual_edits_and_missing_sources(self, post):
        post.return_value = self.response
        save_translation(self.game, self.owner, "Von Hand gepflegt", 0)
        self.game.bgg_description = "Geänderte Quelle."
        self.game.save()
        second = GameEntry.objects.create(event=self.event, name="Zweites", bgg_id=14,
                                          bgg_description="Source text.", created_by=self.owner)
        request_translation(second, self.owner)
        second.bgg_description = ""
        second.save()

        self.assertEqual(refresh_changed_translations(limit=10), [])

        post.assert_called_once()
        self.assertEqual(BggTranslation.objects.get(pk=13).text_de, "Von Hand gepflegt")

    @patch("events.translations.requests.post")
    def test_scheduled_refresh_failure_keeps_previous_text_without_retry(self, post):
        post.return_value = self.response
        request_translation(self.game, self.owner)
        self.game.bgg_description = "Changed."
        self.game.save()
        post.side_effect = requests.Timeout("sensitive content must not be shown")

        outcomes = refresh_changed_translations(limit=10)

        self.assertEqual([(bgg_id, status) for bgg_id, status, _ in outcomes], [(13, "failed")])
        record = BggTranslation.objects.get(pk=13)
        self.assertEqual(record.text_de, "Bauen — handeln.\nNeuer Absatz.")
        self.assertEqual(record.source_text, "Changed.")
        self.assertNotIn("sensitive", record.error_message)
        # Die neue Quellfassung wurde bereits einmal angefragt: kein Wiederholungsversuch.
        post.side_effect = None
        self.assertEqual(refresh_changed_translations(limit=10), [])
        self.assertEqual(post.call_count, 2)

    @patch("events.translations.requests.post")
    def test_scheduled_refresh_repairs_failed_records_after_source_change(self, post):
        post.side_effect = requests.Timeout("boom")
        self.assertEqual(request_translation(self.game, self.owner).status, "failed")
        self.game.bgg_description = "Neue Quelle."
        self.game.save()
        post.side_effect = None
        post.return_value = self.response

        outcomes = refresh_changed_translations(limit=10)

        self.assertEqual([(bgg_id, status) for bgg_id, status, _ in outcomes], [(13, "ready")])
        record = BggTranslation.objects.get(pk=13)
        self.assertEqual(record.text_de, "Bauen — handeln.\nNeuer Absatz.")
        self.assertEqual(record.source_text, "Neue Quelle.")

    @patch("events.translations.requests.post")
    def test_refresh_cannot_overwrite_late_manual_recovery(self, post):
        post.return_value = self.response
        request_translation(self.game, self.owner)
        self.game.bgg_description = "Changed."
        self.game.save()

        def during_request(*args, **kwargs):
            BggTranslation.objects.filter(pk=13).update(attempted_at=timezone.now() - datetime.timedelta(minutes=3))
            save_translation(self.game, self.owner, "Manuelle Wiederherstellung", 2)
            return self.response

        post.side_effect = during_request
        refresh_changed_translations(limit=10)
        record = BggTranslation.objects.get(pk=13)
        self.assertEqual(record.text_de, "Manuelle Wiederherstellung")
        self.assertTrue(record.edited_at)

    @patch("events.translations.requests.post")
    def test_translation_requires_post_and_is_idempotent(self, post):
        post.return_value = self.response
        self.assertContains(self.client.get(self.url), "Auf Deutsch übersetzen")
        post.assert_not_called()
        for _ in range(2):
            self.assertEqual(self.client.post(self.url, {"action": "translate", "bgg_id": 13}).status_code, 302)
        post.assert_called_once()
        result = self.client.get(self.url)
        self.assertContains(result, "Bauen — handeln.")
        self.assertNotContains(result, "test-secret")
        self.assertNotContains(result, 'value="translate"')

    @patch("events.translations.requests.post")
    def test_permission_csrf_and_changed_bgg_checks(self, post):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"action": "translate", "bgg_id": 13}).status_code, 302)
        other = get_user_model().objects.create_user(username="translation-other")
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, {"action": "translate", "bgg_id": 13}).status_code, 403)
        self.client.force_login(self.owner)
        self.client.post(self.url, {"action": "translate", "bgg_id": 999})
        csrf_client = Client(SERVER_NAME="localhost", enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(self.url, {"action": "translate", "bgg_id": 13}).status_code, 403)
        post.assert_not_called()

    @patch("events.translations.requests.post")
    def test_event_access_and_readonly_are_enforced(self, post):
        author = get_user_model().objects.create_user(username="entry-author")
        self.game.created_by = author
        self.game.save()
        self.client.force_login(author)
        self.event.set_access_password("private")
        self.event.save()
        self.assertEqual(self.client.post(self.url, {"action": "translate", "bgg_id": 13}).status_code, 403)
        with patch.object(Event, "is_readonly", new_callable=lambda: property(lambda e: True)):
            self.event.set_access_password("")
            self.event.save()
            self.assertEqual(self.client.post(self.url, {"action": "translate", "bgg_id": 13}).status_code, 403)
        post.assert_not_called()

    def test_attach_uses_one_query_and_does_not_mix_bgg_ids(self):
        BggTranslation.objects.create(bgg_id=13, text_de="Catan")
        games = [self.game, GameEntry(bgg_id=13), GameEntry(bgg_id=999), GameEntry()]
        with self.assertNumQueries(1):
            attach_translations(games)
        self.assertEqual(games[0].bgg_translation, games[1].bgg_translation)
        self.assertIsNone(games[2].bgg_translation)
        self.assertIsNone(games[3].bgg_translation)

    def test_blank_and_stale_form_preserve_saved_text(self):
        saved = save_translation(self.game, self.owner, "Aktuell", 0)
        for text, revision in [("", saved.revision), ("Mein Entwurf", 0)]:
            response = self.client.post(self.url, {"action": "save", "bgg_id": 13, "revision": revision, "text_de": text})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Speichern nicht möglich")
            if text:
                self.assertContains(response, text)
            self.assertEqual(BggTranslation.objects.get(pk=13).text_de, "Aktuell")

    def test_translated_html_is_escaped(self):
        save_translation(self.game, self.owner, '<script>alert("x")</script>\nText', 0)
        detail = self.client.get(f"/{self.event.pk}/")
        printed = self.client.get(f"/{self.event.pk}/spiele/erklaerbaeren/drucken/")
        for response in [detail, printed]:
            self.assertNotContains(response, '<script>alert("x")</script>')
            self.assertContains(response, '&lt;script&gt;')
