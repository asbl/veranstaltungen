import re
from html import unescape
from django.utils.html import strip_tags
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

BGG_THING_URL = "https://boardgamegeek.com/xmlapi2/thing"
BGG_SEARCH_URL = "https://boardgamegeek.com/xmlapi2/search"
BGG_SEARCH_MAX_RESULTS = 8
REQUEST_TIMEOUT = 10
MAX_IDS_PER_REQUEST = 20


class BggLookupError(Exception):
    """Wird ausgelöst, wenn ein Spiel nicht von BGG geladen werden konnte."""


def fetch_bgg_games(bgg_ids):
    ids = list(dict.fromkeys(int(value) for value in bgg_ids))
    if not ids or len(ids) > MAX_IDS_PER_REQUEST:
        raise ValueError(f"Pro BGG-Anfrage sind 1 bis {MAX_IDS_PER_REQUEST} IDs erlaubt.")
    if not settings.BGG_API_TOKEN:
        raise BggLookupError("BGG_API_TOKEN ist nicht konfiguriert; Synchronisierung übersprungen.")
    try:
        response = requests.get(
            BGG_THING_URL,
            params={"id": ",".join(map(str, ids)), "type": "boardgame", "stats": 1},
            headers={"Authorization": f"Bearer {settings.BGG_API_TOKEN}"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code in (202, 429, 500, 503):
            raise BggLookupError("BGG verarbeitet oder begrenzt die Anfrage; sie wird später erneut versucht.")
        response.raise_for_status()
    except requests.RequestException as exc:
        raise BggLookupError(f"BGG war nicht erreichbar: {exc}") from exc

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise BggLookupError("Antwort von BGG konnte nicht gelesen werden.") from exc

    return {int(item.get("id")): _parse_item(item) for item in root.findall("item")}


def _parse_item(item):

    def _int(tag):
        el = item.find(tag)
        if el is None or el.get("value") is None:
            return None
        try:
            return int(el.get("value"))
        except ValueError:
            return None

    name = _primary_name(item) or f"BGG #{item.get('id')}"

    thumbnail_el = item.find("thumbnail")
    thumbnail_url = thumbnail_el.text.strip() if thumbnail_el is not None and thumbnail_el.text else ""

    complexity = None
    weight_el = item.find("statistics/ratings/averageweight")
    if weight_el is not None and weight_el.get("value") is not None:
        try:
            complexity = Decimal(weight_el.get("value")).quantize(Decimal("0.01"))
        except InvalidOperation:
            complexity = None

    rating = None
    rating_el = item.find("statistics/ratings/average")
    if rating_el is not None and rating_el.get("value") is not None:
        try:
            rating = Decimal(rating_el.get("value")).quantize(Decimal("0.01"))
        except InvalidOperation:
            pass

    description_el = item.find("description")
    description = description_el.text.strip() if description_el is not None and description_el.text else ""
    categories = [link.get("value") for link in item.findall("link[@type='boardgamecategory']") if link.get("value")]
    mechanics = [link.get("value") for link in item.findall("link[@type='boardgamemechanic']") if link.get("value")]

    return {
        "name": name,
        "min_players": _int("minplayers"),
        "max_players": _int("maxplayers"),
        "playtime_minutes": _int("playingtime"),
        "min_age": _int("minage"),
        "year": _int("yearpublished"),
        "complexity": complexity,
        "rating": rating,
        "description": description,
        "categories": categories,
        "mechanics": mechanics,
        "thumbnail_url": thumbnail_url,
    }


def fetch_bgg_game(bgg_id):
    games = fetch_bgg_games([bgg_id])
    if int(bgg_id) not in games:
        raise BggLookupError(f"Kein Spiel mit BGG-ID {bgg_id} gefunden.")
    return games[int(bgg_id)]


BGG_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?boardgamegeek\.com/(?:boardgame\w*|thing)/(\d+)", re.I)


def bgg_id_from_link(text):
    """Erkennt einen eingefügten BGG-Link. Reine Zahlen bleiben Suchbegriffe,
    weil es Spiele wie „1830“ gibt."""
    match = BGG_LINK_RE.match((text or "").strip())
    return int(match.group(1)) if match else None


def _primary_name(item):
    # Nicht mit `or` verketten: ein Element ohne Kindelemente ist falsy.
    name_el = item.find("name[@type='primary']")
    if name_el is None:
        name_el = item.find("name")
    return name_el.get("value") if name_el is not None else ""


def _search_rank(name, query):
    """BGG liefert Treffer alphabetisch; exakte Titel sollen vorne stehen,
    sonst landet z. B. bei „Omens“ zuerst „18 Omens“ und das gesuchte
    Spiel fällt aus den ersten Treffern heraus."""
    name, query = name.casefold(), query.casefold()
    if name == query:
        return 0
    if name.startswith(query + ":") or name.startswith(query + " "):
        return 1
    if name.startswith(query):
        return 2
    if re.search(r"\b" + re.escape(query), name):
        return 3
    return 4


def search_bgg_games(query):
    """Sucht Spiele nach Titel und liefert Treffer mit ID, Name und Erscheinungsjahr.
    Ein eingefügter BGG-Link liefert genau dieses Spiel."""
    query = (query or "").strip()
    linked_id = bgg_id_from_link(query)
    if linked_id is not None:
        try:
            game = fetch_bgg_game(linked_id)
        except ValueError:
            return []
        return [{"id": linked_id, "name": game["name"], "year": str(game["year"] or "")}]
    if len(query) < 2:
        return []
    if not settings.BGG_API_TOKEN:
        raise BggLookupError("BGG_API_TOKEN ist nicht konfiguriert.")
    try:
        response = requests.get(
            BGG_SEARCH_URL,
            params={"query": query, "type": "boardgame"},
            headers={"Authorization": f"Bearer {settings.BGG_API_TOKEN}"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code in (202, 429, 500, 503):
            raise BggLookupError("BGG verarbeitet oder begrenzt die Anfrage; bitte später erneut versuchen.")
        response.raise_for_status()
    except requests.RequestException as exc:
        raise BggLookupError(f"BGG war nicht erreichbar: {exc}") from exc

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise BggLookupError("Antwort von BGG konnte nicht gelesen werden.") from exc

    results = []
    for item in root.findall("item"):
        name = _primary_name(item)
        if not name:
            continue
        year_el = item.find("yearpublished")
        results.append({"id": int(item.get("id")), "name": name, "year": year_el.get("value") if year_el is not None else ""})
    # Stabil sortiert: innerhalb einer Stufe bleibt die BGG-Reihenfolge.
    results.sort(key=lambda result: _search_rank(result["name"], query))
    return results[:BGG_SEARCH_MAX_RESULTS]


def description_text(value):
    """Decode BGG entities and preserve paragraph/list breaks as plain text.

    Return an ordinary string so Django still escapes any remaining markup.
    """
    text = unescape(str(value or ""))
    text = re.sub(r"<\s*br\s*/?\s*>|</\s*(?:p|div|li|ul|ol|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*li(?:\s[^>]*)?>", "• ", text, flags=re.I)
    return strip_tags(text).strip()

