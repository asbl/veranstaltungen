"""Spieleempfehlungen für die Übersicht der Druckansicht.

Die Kategorien werden aus den BGG-Daten der Spiele abgeleitet (Spielerzahl,
Dauer, Mindestalter, Komplexität, Kategorien und Mechanismen). Die Grenzen
der Komplexität entsprechen GameEntry.complexity_label (einfach < 2,
komplex ab 3,5), damit Übersicht und Spielkarten zusammenpassen.

Damit die Übersicht bei großen Spielelisten auf eine Druckseite passt, nennt
jede Kategorie höchstens MAX_PER_GROUP Spiele: zuerst Spiele mit Erklärbär,
dann nach BGG-Wertung. Spiele ohne Komplexitätswert haben auf BGG kaum
Bewertungen; ihr Durchschnitt ist zufällig, deshalb stehen sie hinten. Die
übrigen Treffer werden nur gezählt.
"""
from decimal import Decimal

EASY = Decimal("2")
COMPLEX = Decimal("3.5")
MAX_PER_GROUP = 10


def _has(game, field, *names):
    return any(name in (getattr(game, field, None) or []) for name in names)


def _plays_with(game, count):
    return bool(game.min_players and game.max_players and game.min_players <= count <= game.max_players)


def _complexity(game):
    # BGG meldet 0, solange niemand die Komplexität bewertet hat.
    return game.complexity if game.complexity else None


def _complexity_below(game, limit):
    return _complexity(game) is not None and _complexity(game) < limit


CATEGORIES = [
    ("Leicht für große Runden", "ab 6 Personen, einfache Regeln",
     lambda g: (g.max_players or 0) >= 6 and _complexity_below(g, EASY)),
    ("Große Runden mit mehr Tiefgang", "ab 6 Personen, mittlere Komplexität oder mehr",
     lambda g: (g.max_players or 0) >= 6 and _complexity(g) is not None and _complexity(g) >= EASY),
    ("Partyspiele", None, lambda g: _has(g, "bgg_categories", "Party Game")),
    # Ohne BGG-Daten zur besten Spielerzahl: Spiele für kleine Runden (max. 4)
    # funktionieren meist auch zu zweit gut, Partyspiele ab 2 dagegen kaum.
    ("Zu zweit", "für 2 bis höchstens 4 Personen ausgelegt",
     lambda g: bool(g.min_players and g.min_players <= 2 and g.max_players and g.max_players <= 4)),
    ("Für 5 Personen", None, lambda g: _plays_with(g, 5)),
    ("Kooperativ", "alle gemeinsam gegen das Spiel",
     lambda g: _has(g, "bgg_mechanics", "Cooperative Game")),
    ("Teamspiele", None, lambda g: _has(g, "bgg_mechanics", "Team-Based Game")),
    ("Mit Kindern", "ab höchstens 8 Jahren",
     lambda g: bool(g.min_age and g.min_age <= 8) or _has(g, "bgg_categories", "Children's Game")),
    ("Familienspiele", "einfache Regeln",
     lambda g: _complexity_below(g, EASY) and not _has(g, "bgg_categories", "Children's Game")),
    ("Kennerspiele", "mittlere Komplexität",
     lambda g: _complexity(g) is not None and EASY <= _complexity(g) < COMPLEX),
    ("Komplexe Spiele", "für Erfahrene",
     lambda g: _complexity(g) is not None and _complexity(g) >= COMPLEX),
    ("Schnelle Spiele", "bis 30 Minuten",
     lambda g: bool(g.playtime_minutes and g.playtime_minutes <= 30)),
    ("Abendfüllend", "ab 2 Stunden",
     lambda g: bool(g.playtime_minutes and g.playtime_minutes >= 120)),
    ("Bluffen, Verhandeln & Deduktion", None,
     lambda g: _has(g, "bgg_categories", "Bluffing", "Negotiation", "Deduction")),
    ("Wort- & Kommunikationsspiele", None,
     lambda g: _has(g, "bgg_categories", "Word Game") or _has(g, "bgg_mechanics", "Communication Limits")),
    ("Geschicklichkeit", None, lambda g: _has(g, "bgg_categories", "Action / Dexterity")),
]


def _has_explainer(game):
    claims = getattr(game, "explain_claims", None)
    return bool(claims is not None and claims.all())


def _rank(game):
    return (not _has_explainer(game), _complexity(game) is None, -(game.bgg_rating or 0))


def game_recommendations(games, limit=MAX_PER_GROUP):
    """Liefert [(Titel, Hinweis, [Spiele], weitere)] ohne leere Kategorien.
    Die ausgewählten Spiele behalten die übergebene Reihenfolge (in der
    Druckansicht A–Z); „weitere“ zählt die nicht genannten Treffer."""
    groups = []
    for title, hint, matches in CATEGORIES:
        selected = [game for game in games if matches(game)]
        if selected:
            chosen = {id(game) for game in sorted(selected, key=_rank)[:limit]}
            shown = [game for game in selected if id(game) in chosen]
            groups.append((title, hint, shown, len(selected) - len(shown)))
    return groups
