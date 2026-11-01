from django.contrib import admin
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import include, path


def _passwordless_notice(request, *args, **kwargs):
    """Diese App kennt keine Passwörter (Login per E-Mail-Code) - die
    entsprechenden allauth-Routen (Reset/Ändern/Setzen) werden hier
    abgefangen, bevor sie allauth erreichen, damit niemand in eine
    funktionslose Sackgasse läuft."""
    messages.info(request, "Diese Seite ist passwortlos: Melde dich einfach mit deiner E-Mail-Adresse an.")
    return redirect("account_login")


def _signup_via_login_code(request, *args, **kwargs):
    """Registrierung läuft über denselben Anmeldecode wie der Login (siehe
    events.account_forms) - eine separate Signup-Seite mit eigenem
    Bestätigungslink würde wieder zwei getrennte Schritte (Link bestätigen,
    danach erneut Code anfordern) einführen, statt in einem Schritt
    anzulegen, zu bestätigen und einzuloggen."""
    messages.info(request, "Diese Seite ist passwortlos: Gib einfach deine E-Mail-Adresse ein - ein neues Konto wird automatisch angelegt.")
    return redirect("account_login")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/password/reset/", _passwordless_notice),
    path("accounts/password/change/", _passwordless_notice),
    path("accounts/password/set/", _passwordless_notice),
    path("accounts/signup/", _signup_via_login_code),
    path("accounts/", include("allauth.urls")),
    path("", include("events.urls")),
]
