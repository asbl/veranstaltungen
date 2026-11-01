# Veranstaltungen

Self-hostbares Organisations-Tool für Veranstaltungen eines Vereins oder
Anbieters: Anmeldungen, Mitbringliste, Dienste, Spielelisten (mit
BoardGameGeek-Abgleich), Ideen, Fragen, Orga-Planung und Druckansichten -
passwortlos per E-Mail-Anmeldecode.

Das Tool ist bewusst **mandantenfähig im Branding**: Vereinsname, Logo und
Impressumsdaten liegen nicht im Code, sondern pro Instanz in der Datenbank
bzw. in lokalen Dateien/Umgebungsvariablen. Dadurch kann derselbe Code von
beliebigen Anbietern selbst gehostet werden.

## Schnellstart (Docker Compose)

Voraussetzungen: Docker mit Compose, ein laufender Traefik-Reverse-Proxy mit
ACME (z. B. `alpnresolver`) und ein externes Docker-Netz `veranstaltungen_proxy`:

```bash
docker network create veranstaltungen_proxy   # falls nicht vorhanden
cp .env.example .env                          # und Werte anpassen (siehe unten)
docker compose up -d --build
```

Der Web-Container führt beim Start automatisch `migrate` und `collectstatic`
aus. Ein Cron-Container ruft alle 5 Minuten die Management-Commands
(`sync_bgg_games`, `send_idea_comment_notifications`, `prune_unconfirmed_accounts`,
`auto_translate_games`, `send_error_report`) auf.

Ohne Traefik kannst du stattdessen direkt auf den Port zugreifen, z. B. mit
einem `docker-compose.override.yml` (gitignored), der `ports: ["8000:8000"]`
und `SITE_DOMAIN=localhost` setzt.

## Konfiguration (`.env`)

| Variable | Bedeutung |
| --- | --- |
| `SITE_DOMAIN` | Domain der Instanz, z. B. `events.example.org` (Traefik-Hostregel, E-Mail-Links, `Site`-Eintrag) |
| `DJANGO_SECRET_KEY` | Pflicht in Produktion (`DJANGO_DEBUG=0`); z. B. `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DJANGO_DEBUG` | `0` in Produktion |
| `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS` | Hosts der Instanz |
| `SITE_NAME` (Alias `ORGANIZATION_NAME`) | Name des Vereins/Anbieters (Titel, Header, E-Mail-Betreff) |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Datenbank-Zugang |
| `EMAIL_BACKEND_MODE` | `console` (Dev) oder `smtp` (Produktion) + `EMAIL_*` |
| `DEFAULT_FROM_EMAIL` | Absender der Systemmails |
| `ERROR_REPORT_EMAILS` | Empfänger des Fehlerberichts (Cron) |
| `IMPRINT_NAME/STREET/CITY/EMAIL` | Betreiberangaben für Impressum/Datenschutz - nur lokal in `.env`, nie im Repo |
| `BGG_API_TOKEN` | optionales BoardGameGeek-API-Token |
| `DEEPL` | optionaler DeepL-Key für Übersetzungen von Spielbeschreibungen |

## Branding anpassen (Name & Logo)

- Im Admin (als Staff/Superuser) unter **„Betreiber-Einstellungen“**: Name
  setzen und Logo hochladen. Das Logo liegt dann unter `media/site/` (gitignored)
  und wird öffentlich über `/logo/` ausgeliefert.
- Alternativ `SITE_NAME` in der `.env` setzen (beim ersten Start per Migration
  in die Datenbank übernommen).
- Ohne hochgeladenes Logo wird ein neutraler Platzhalter
  (`events/static/events/img/logo.svg`) angezeigt.

## Entwicklung

```bash
docker compose exec web python manage.py test        # Testsuite (365 Tests)
docker compose exec web python manage.py shell
docker compose logs -f web                           # Login-Codes landen im Dev-Log
```

Stack: Django 5, django-allauth (passwortloser E-Mail-Code-Login), PostgreSQL,
WhiteNoise, Gunicorn, openpyxl (Teilnehmer-Export), requests (BGG-API).

## Verzeichnisstruktur

```
config/            Django-Projekt (settings, urls, wsgi)
events/            App: Modelle, Views, Templates, Migrationen, Management-Commands
templates/         Projekt-Templates (base.html, allauth, legal, account)
docs/              Doku zu einzelnen Features
scripts/           Betreiber-Skripte (z. B. DB-Backup)
```

Hinweis: `events/management/commands/prepare_taunus26.py` und der Abschnitt
in `docs/public-events.md` sind ein **Beispiel** eines einmaligen
Vorbereitungslaufs für eine konkrete Veranstaltung des ursprünglichen
Betreibers - für neue Instanzen nicht nötig (läuft ohne passende Daten als
No-op).

## Datenschutz

Es gibt keine Werbung, Analyse oder Tracking-Cookies. Details stehen im
Datenschutz-Dialog der Anwendung (siehe `templates/legal/footer.html`).
Sicherheitshinweise und Meldung von Schwachstellen: siehe [SECURITY.md](SECURITY.md).

## Lizenz

[MIT](LICENSE)
