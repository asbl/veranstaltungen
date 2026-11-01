"""Available event lists and backwards-compatible display defaults."""
LISTS = [
    ("attendance", "Anwesenheit", "users"),
    ("potluck", "Mitbringliste", "basket-shopping"),
    ("advert", "Werbung", "bullhorn"),
    ("orga", "Verantwortlichkeiten-Liste", "list-check"),
    ("members", "TODO-Liste", "list-check"),
    ("duties", "Dienste", "clipboard-check"),
    ("ideas", "Ideen", "lightbulb"),
    ("questions", "Fragen", "circle-question"),
    ("slots", "Angebote", "calendar-day"),
    ("games", "Spiele", "dice"),
]
PLANNING_LISTS = {"advert", "orga", "members"}


def configured_lists(settings):
    definitions = {key: (label, icon) for key, label, icon in LISTS}
    saved = {}
    if isinstance(settings, list):
        for row in settings:
            if isinstance(row, dict) and row.get("key") in definitions:
                saved[row["key"]] = row
    for key in list(saved) + [key for key in definitions if key not in saved]:
        row = saved.get(key, {})
        label, icon = definitions[key]
        yield {"key": key, "label": label, "icon": icon,
               "enabled": row.get("enabled", True), "description": row.get("description", ""),
               "show_status": row.get("show_status", True),
               "planning": key in PLANNING_LISTS,
               "template": "events/_list_" + key + ".html"}
