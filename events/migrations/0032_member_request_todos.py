from django.db import migrations


REQUESTS = {
    "Kuchenanfrage an die Mitglieder": "Reminder: Kuchenanfrage an die Mitglieder",
    "Dienstanfrage an die Mitglieder (2-Stunden-Schichten)": "Reminder: Dienstanfrage an die Mitglieder",
    "Dienstanfrage für Abbau": "Reminder: Dienstanfrage für Abbau",
}


def move_requests(apps, schema_editor):
    Item = apps.get_model("events", "PlanningItem")
    db = schema_editor.connection.alias
    for item in Item.objects.using(db).filter(category="orga", title__in=REQUESTS):
        item.category = "members"
        item.save(using=db, update_fields=["category"])
        Item.objects.using(db).get_or_create(
            event_id=item.event_id, category="members", title=REQUESTS[item.title],
            defaults={"people": item.people},
        )


class Migration(migrations.Migration):
    dependencies = [("events", "0031_planningitem_weeks_before_and_more")]
    operations = [migrations.RunPython(move_requests, migrations.RunPython.noop)]
