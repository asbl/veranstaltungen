import os

from django import template

register = template.Library()


@register.inclusion_tag("legal/footer.html")
def legal_footer():
    """Render operator details supplied by the process environment."""
    return {
        "imprint": {
            "name": os.environ.get("IMPRINT_NAME", ""),
            "street": os.environ.get("IMPRINT_STREET", ""),
            "city": os.environ.get("IMPRINT_CITY", ""),
            "email": os.environ.get("IMPRINT_EMAIL", ""),
        }
    }
