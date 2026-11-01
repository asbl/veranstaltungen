from datetime import timedelta

from allauth.account.adapter import get_adapter
from allauth.account.forms import LoginForm, RequestLoginCodeForm
from allauth.account.internal.flows.login_by_code import LoginCodeVerificationProcess
from allauth.account.models import EmailAddress
from allauth.account.utils import filter_users_by_email, setup_user_email
from allauth.core import context
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import is_known_person


class _AutoSignupData:
    """Stellt nur das bereit, was adapter.save_user() aus einem echten
    Signup-Formular ausliest (siehe allauth BaseSignupForm.save)."""

    def __init__(self, email):
        self.cleaned_data = {"email": email}
        self._has_phone_field = False


class RequestLoginCodeOrSignupForm(RequestLoginCodeForm):
    """Fordert jemand einen Anmeldecode für eine E-Mail-Adresse ohne Konto an,
    wird das Konto hier sofort mit angelegt statt nur eine separate
    Registrierungs-Mail zu verschicken. Ein einziger Code registriert,
    bestätigt die Adresse und loggt ein - ohne den Umweg "erst registrieren,
    dann noch einmal Code anfordern".
    """

    def clean_email(self):
        email = super().clean_email()
        if email and not self._user:
            self._user = self._get_or_create_user(email)
        return email

    def _get_or_create_user(self, email):
        """User.email/EmailAddress.email sind nicht DB-unique (ACCOUNT_UNIQUE_EMAIL
        prüft nur das hier umgangene normale Signup-Formular) - zwei fast
        gleichzeitige Anfragen für dieselbe neue Adresse könnten also sonst zwei
        Konten anlegen. Deshalb wird nach dem Anlegen erneut nachgeschaut:
        Existieren jetzt mehrere Konten für die Adresse, gewinnt deterministisch
        das zuerst angelegte (kleinste pk) und alle anderen - die zwangsläufig
        ebenfalls gerade erst und ohne bestätigte Mail angelegt wurden, tragen
        also garantiert noch keine Daten - werden sofort wieder gelöscht."""
        request = context.request
        adapter = get_adapter()
        user = adapter.new_user(request)
        adapter.save_user(request, user, _AutoSignupData(email))
        setup_user_email(request, user, [])
        candidates = filter_users_by_email(email, is_active=True, prefer_verified=True)
        survivor = min(candidates, key=lambda u: u.pk) if candidates else user
        for candidate in candidates:
            if candidate.pk != survivor.pk:
                candidate.delete()
        return survivor


class EventAccessLoginCodeForm(RequestLoginCodeForm):
    """Anmeldecode-Anfrage am Zugang einer passwortgeschützten Veranstaltung.

    Anders als beim normalen Login (RequestLoginCodeOrSignupForm) werden hier
    keine Konten für unbekannte Adressen angelegt und es wird nur für
    „bekannte Personen“ (Teilnahme an einer beendeten Veranstaltung, siehe
    is_known_person) tatsächlich ein Code verschickt. Die Antwort fällt für
    alle Adressen identisch aus, damit sich Teilnehmende nicht aufzählen
    lassen (siehe ACCOUNT_PREVENT_ENUMERATION); übernimmt zusätzlich allauths
    Rate-Limit für Code-Anfragen („request_login_code“ je E-Mail und IP)."""

    def clean_email(self):
        email = super().clean_email()
        if email and getattr(self, "_user", None) and not is_known_person(self._user):
            # Konto existiert, ist aber keine bekannte Person: neutral bleiben
            # und keinen Code verschicken (die View antwortet identisch).
            self._user = None
        return email


def initiate_login_code(request, user, email):
    """Startet den Login-Code-Flow von allauth für eine bekannte Person - wie
    es allauths RequestLoginCodeView.form_valid tut: verschickt den Code per
    E-Mail und hinterlegt die laufende Anmeldung in der Session, sodass die
    Bestätigungsseite von allauth (account_confirm_login_code) ihn abfragt
    und danach zur zurücklegenden „next“-URL (der Veranstaltung) leitet.
    Kapselt allauth-Interna bewusst an einer Stelle."""
    LoginCodeVerificationProcess.initiate(request=request, user=user, email=email)


class LoginOrSignupForm(LoginForm):
    """Ohne Passwortfeld (siehe ACCOUNT_SIGNUP_FIELDS) läuft das normale
    "Anmelden"-Formular auf der Login-Seite intern selbst über eine
    Login-Code-Anfrage (LoginForm._clean_without_password in allauth).
    Dabei verwendet allauth aber fest sein eigenes RequestLoginCodeForm statt
    des über ACCOUNT_FORMS registrierten - hier wird das nachgeholt, damit
    auch der Haupt-Login-Button (nicht nur "Anmeldecode senden") unbekannte
    E-Mail-Adressen direkt registriert statt in die alte Sackgasse zu laufen.
    """

    def _clean_without_password(self, email, phone):
        data = {}
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        if not data:
            self.add_error("login", get_adapter().validation_error("invalid_login"))
        else:
            form = RequestLoginCodeOrSignupForm(data)
            if not form.is_valid():
                for field in ["phone", "email"]:
                    errors = form.errors.get(field) or []
                    for error in errors:
                        self.add_error("login", error)
            else:
                self.user = form._user
        return self.cleaned_data


def prune_unconfirmed_accounts(days=7):
    """Löscht Konten, die RequestLoginCodeOrSignupForm beim Anfordern eines
    Anmeldecodes für eine unbekannte Adresse automatisch angelegt hat, bei
    denen aber nie der zugeschickte Code eingegeben wurde (siehe
    prune_unconfirmed_accounts-Management-Command). Ohne bestätigte
    E-Mail-Adresse konnte sich so ein Konto nie einloggen und hat daher
    garantiert keine Daten angelegt - das Löschen ist also unbedenklich.
    Gibt die Anzahl der gelöschten Konten zurück."""
    cutoff = timezone.now() - timedelta(days=days)
    candidates = (
        get_user_model()
        .objects.filter(date_joined__lt=cutoff, is_staff=False, is_superuser=False)
        .exclude(pk__in=EmailAddress.objects.filter(verified=True).values("user_id"))
    )
    count = candidates.count()
    candidates.delete()
    return count
