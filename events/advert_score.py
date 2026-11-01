"""Count completed advertising actions once per person and action."""
import re
from .models import get_display_name


def split_names(value):
    return [name.strip() for name in re.split(r"[,;\n&]+|\s+und\s+", value, flags=re.IGNORECASE) if name.strip()]


def name_key(value):
    return " ".join(value.split()).casefold()


def highscore(items, identities):
    mapping = {}
    for identity in identities:
        for name in [identity.name, *split_names(identity.aliases)]:
            mapping[name_key(name)] = identity.name
    scores = {}
    for item in items:
        if item.everyone:
            names = []
            for user in item.completed_by.all():
                display = get_display_name(user)
                names.append(mapping.get(name_key(display), mapping.get(name_key(user.username), display)))
        elif item.status == "done":
            names = split_names(item.people)
        else:
            continue
        action_people = {}
        for name in names:
            canonical = mapping.get(name_key(name), name)
            key = name_key(canonical)
            action_people[key] = canonical
        for key, name in action_people.items():
            row = scores.setdefault(key, {"name": name, "points": 0})
            row["points"] += 1
    rows = sorted(scores.values(), key=lambda row: (-row["points"], name_key(row["name"])))
    previous = None
    for index, row in enumerate(rows, 1):
        if row["points"] != previous:
            rank = index
        row["rank"] = rank
        previous = row["points"]
    return rows
