import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# `manage.py test` läuft über den Django-Testclient per WSGI ohne echten
# Reverse-Proxy davor - der würde von SECURE_SSL_REDIRECT auf jeden POST/GET
# mit einem 301 auf https:// antworten, noch bevor die View überhaupt läuft.
RUNNING_TESTS = "test" in sys.argv

# Domain dieser Instanz (Betreiber-spezifisch, daher per ENV konfiguriert).
SITE_DOMAIN = os.environ.get("SITE_DOMAIN", "localhost")

DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

# Im Produktivbetrieb (DEBUG=0) darf kein Fallback-Schlüssel verwendet werden.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or RUNNING_TESTS:
        SECRET_KEY = "dev-insecure-secret-key-change-me"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY muss gesetzt sein, wenn DJANGO_DEBUG=0 (siehe .env.example)."
        )
ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", f"{SITE_DOMAIN},localhost,127.0.0.1"
).split(",")
CSRF_TRUSTED_ORIGINS = os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS", f"https://{SITE_DOMAIN}"
).split(",")

# Server steht hinter einem bestehenden Reverse-Proxy, der TLS terminiert
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Die Seite ist ausschließlich über HTTPS erreichbar (Traefik terminiert TLS
# und setzt X-Forwarded-Proto korrekt) - daher Session-/CSRF-Cookies auf
# "secure" beschränken und HTTP->HTTPS-Redirect + HSTS aktivieren.
SECURE_SSL_REDIRECT = not RUNNING_TESTS
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 7
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "events",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "events.middleware.RequestAuditMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "events.context_processors.site_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "veranstaltungen"),
        "USER": os.environ.get("POSTGRES_USER", "veranstaltungen"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "veranstaltungen"),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "TEST": {"NAME": os.environ.get("DJANGO_TEST_DATABASE_NAME")},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "de-de"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Private uploads are served through the event access check, never as static files.
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Dateibasiert statt LocMemCache, damit der Freischalt-Status einer
# Veranstaltung (siehe events.views.event_detail) über alle Gunicorn-Worker
# hinweg geteilt wird und Neustarts übersteht.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": BASE_DIR / "cache",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Passwortlose Anmeldung per E-Mail: Login-Code wird per Mail verschickt
# (echte "Magic Link"-UX ist Ausbaustufe fuer spaeter, siehe Roadmap)
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_UNIQUE_EMAIL = True
# Wer den Bestätigungslink im selben Browser öffnet, in dem er sich registriert
# hat, wird direkt eingeloggt statt zusätzlich noch einen Login-Code per Mail
# anfordern zu müssen. Auf einem anderen Gerät/Browser funktioniert der
# bisherige Ablauf (E-Mail erneut eingeben -> Code) unverändert als Fallback.
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
# Wird auf der Login-Seite ein Anmeldecode für eine E-Mail-Adresse ohne Konto
# angefordert, legt dieses Formular das Konto sofort mit an, statt nur eine
# separate Registrierungs-Mail zu verschicken. Ein einziger Code registriert,
# bestätigt und loggt ein (siehe events/account_forms.py). Vermeidet den
# Support-Fall "Code kommt nie an", weil Nutzer sonst erst per Mail-Link ein
# Konto anlegen und danach ein zweites Mal einen Code anfordern mussten.
ACCOUNT_FORMS = {
    "login": "events.account_forms.LoginOrSignupForm",
    "request_login_code": "events.account_forms.RequestLoginCodeOrSignupForm",
}

LOGIN_REDIRECT_URL = "/"

# EMAIL_BACKEND_MODE=smtp sobald ein SMTP-Relay konfiguriert ist, sonst landen
# Login-Codes nur im Container-Log (docker compose logs web)
if os.environ.get("EMAIL_BACKEND_MODE", "console") == "smtp":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", f"veranstaltungen@{SITE_DOMAIN}")

# Token einer unter https://boardgamegeek.com/applications registrierten und
# genehmigten Anwendung. Er bleibt ausschließlich auf dem Server. In der ENV
# wird er als BGG_API_TOKEN oder kurz als BGG gepflegt.
BGG_API_TOKEN = os.environ.get("BGG_API_TOKEN") or os.environ.get("BGG", "")
DEEPL_API_KEY = (os.environ.get("DEEPL_API_KEY") or os.environ.get("DEEPL", "")).strip()

# Technisches Protokoll: Formular-Absendungen, Fehlerantworten und Tracebacks
# landen als JSON-Zeilen in logs/app.jsonl (auf dem Host
# projects/veranstaltungen/logs/), zusätzlich wie bisher in
# `docker compose logs web`. Der cron-Container verschickt neue Warnungen und
# Fehler alle 5 Minuten gesammelt an ERROR_REPORT_EMAILS (send_error_report)
# und löscht rotierte Tagesdateien nach APP_LOG_RETENTION_DAYS Tagen.
APP_LOG_FILE = os.environ.get("APP_LOG_FILE", str(BASE_DIR / "logs" / "app.jsonl"))
# Host-Pfad derselben Datei für Verweise in Fehlermails (nur Anzeige, ENV-steuerbar).
APP_LOG_HOST_PATH = os.environ.get("APP_LOG_HOST_PATH", APP_LOG_FILE)
APP_LOG_RETENTION_DAYS = 90
ERROR_REPORT_EMAILS = [
    address.strip() for address in os.environ.get("ERROR_REPORT_EMAILS", "").split(",") if address.strip()
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "jsonl": {"()": "events.app_log.JsonLineFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
        # Tests schreiben nie ins Live-Protokoll (gleicher Container, gleiches Volume).
        "app_file": {"class": "logging.NullHandler"} if RUNNING_TESTS else {
            "class": "logging.handlers.WatchedFileHandler",
            "filename": APP_LOG_FILE,
            "formatter": "jsonl",
            "encoding": "utf-8",
        },
    },
    "root": {
        "handlers": ["console", "app_file"],
        "level": "ERROR",
    },
    "loggers": {
        "django.request": {
            "handlers": ["console", "app_file"],
            "level": "ERROR",
            "propagate": False,
        },
        # z. B. „CSRF-Cookie fehlt“ – der Grund steht nur hier.
        "django.security": {
            "handlers": ["console", "app_file"],
            "level": "WARNING",
            "propagate": False,
        },
        "events.audit": {
            "handlers": ["app_file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
