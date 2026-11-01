from django.db import migrations


TASKS = [
    ("Veranstaltungsraum organisieren", "year"),
    ("Planungstreffen organisieren", "6_8"),
    ("Spiele-Erklärerliste drucken und mitnehmen", "short"),
]


def group_todos(apps, schema_editor):
    Item = apps.get_model("events", "PlanningItem")
    Event = apps.get_model("events", "Event")
    db = schema_editor.connection.alias
    for item in Item.objects.using(db).filter(category="members", todo_period=""):
        weeks = item.weeks_before
        if weeks is not None:
            item.todo_period = "year" if weeks > 8 else "6_8" if weeks >= 6 else "4_5" if weeks >= 4 else "2_3" if weeks >= 2 else "short"
        else:
            item.todo_period = "2_3" if item.title.startswith("Reminder:") else "4_5"
        item.save(using=db, update_fields=["todo_period"])
    for event in Event.objects.using(db).filter(title="Taunus spielt 26", planning_enabled=True):
        for title, period in TASKS:
            Item.objects.using(db).get_or_create(
                event_id=event.pk, category="members", title=title,
                defaults={"todo_period": period, "people": "Orga"},
            )


class Migration(migrations.Migration):
    dependencies = [("events", "0033_planningitem_todo_period_alter_planningitem_category")]
    operations = [migrations.RunPython(group_todos, migrations.RunPython.noop)]
