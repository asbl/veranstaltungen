import re
from urllib.parse import quote

from django import template
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe

from ..models import get_display_name

register = template.Library()


@register.filter
def bgg_description_text(value):
    from ..bgg import description_text

    return description_text(value)


@register.simple_tag(takes_context=True)
def action_url(context, url_name, *args):
    """URL for a POST-only action that has no GET view of its own. Logged-in
    users get the real endpoint; anonymous visitors get sent to login first,
    with `next` pointing back to the current page (not the POST-only URL,
    which would 405 on the way back)."""
    request = context["request"]
    target = reverse(url_name, args=args)
    if request.user.is_authenticated:
        return target
    return f"{reverse('account_login')}?next={quote(request.path)}"


@register.filter
def display_name(user):
    if user is None:
        return "Unbekannt"
    return get_display_name(user)


@register.inclusion_tag("events/_user_link.html")
def user_link(user, event, label=None):
    """Render event-scoped user references consistently.

    ``label`` is useful for attendances whose event-specific name differs
    from the account-wide display name. Deleted users remain plain text.
    """
    if user is None:
        return {"user": None, "label": label or "Unbekannt"}
    return {
        "user": user,
        "event": event,
        "label": label or get_display_name(user),
    }


@register.filter
def get_item(d, key):
    return d.get(key) if d else None


@register.filter
def simple_markdown(text):
    """Escape user input first, then apply a tiny, safe markdown-ish subset
    (**bold**, *italic*, "- " bullet lists) - no raw HTML can ever slip
    through since escaping happens before any tag is introduced."""
    if not text:
        return ""
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    out = []
    in_list = False
    for line in text.split("\n"):
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            if line.strip():
                out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return mark_safe("".join(out))


@register.filter
def simple_markdown_inline(text):
    """Safe inline variant for compact previews inside phrasing-only HTML."""
    if not text:
        return ""
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    lines = [f"• {line[2:]}" if line.startswith("- ") else line for line in text.split("\n")]
    return mark_safe("<br>".join(lines))


@register.filter
def bgg_german_labels(values):
    from ..bgg_labels import german_labels

    return german_labels(values)


@register.filter
def responsible_user(item, user):
    return item.has_responsible_user(user)
