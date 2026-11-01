from .models import SiteSettings


def site_settings(request):
    """Betreiber-Branding (Name/Logo aus der Datenbank) in allen Templates."""
    return {"site_settings": SiteSettings.load()}
