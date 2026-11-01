"""Reusable scheduling built on the existing duty and signup models."""
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Duty


def slot_bounds(start_time, end_time, slot_hours):
    """Split the range [start_time, end_time) into consecutive, equally long
    time slots; the last one is clipped to end_time. Returns (start, end)
    time pairs: 10–18 Uhr with 2 hours becomes
    [(10:00, 12:00), (12:00, 14:00), (14:00, 16:00), (16:00, 18:00)]."""
    minutes = float(slot_hours) * 60
    if minutes < 15 or minutes > 1440 or start_time >= end_time:
        raise ValidationError("Ungültige Schichtdauer oder Zeitspanne.")
    start = datetime.combine(date(2000, 1, 1), start_time)
    end = datetime.combine(date(2000, 1, 1), end_time)
    slots = []
    while start < end:
        stop = min(start + timedelta(minutes=minutes), end)
        slots.append((start.time(), stop.time()))
        start = stop
    return slots


@transaction.atomic
def create_duty_schedule(*, event, title, start_time, end_time, slot_hours, day=None,
                         needed_people=None, description="", category="service", created_by=None):
    """Create one duty series (a single title) across all time slots."""
    if day and day.event_id != event.pk:
        raise ValidationError("Der Tag gehört nicht zu dieser Veranstaltung.")
    return [
        Duty.objects.create(
            event=event, title=title, day=day, start_time=start, end_time=stop,
            needed_people=needed_people, description=description, category=category, created_by=created_by,
        )
        for start, stop in slot_bounds(start_time, end_time, slot_hours)
    ]


@transaction.atomic
def create_duty_grid(*, event, titles, start_time, end_time, slot_hours, day=None,
                     needed_people=None, create_reserve=True, reserve_title="Springer",
                     category="service", created_by=None):
    """Create a full duty grid for a time raster: every duty title is created
    once per time slot (e.g. 10–12, 12–14, ...), plus an additional reserve
    duty per slot ("Springer"). Auf-/Abbau are single duties outside the
    raster and therefore use create_duty_schedule or plain creation instead."""
    if day and day.event_id != event.pk:
        raise ValidationError("Der Tag gehört nicht zu dieser Veranstaltung.")
    titles = [title for title in (title.strip() for title in titles) if title]
    if not titles:
        raise ValidationError("Mindestens ein Dienst ist erforderlich.")
    slots = slot_bounds(start_time, end_time, slot_hours)
    duties = []
    for title in titles:
        for start, stop in slots:
            duties.append(Duty.objects.create(
                event=event, title=title, day=day, start_time=start, end_time=stop,
                needed_people=needed_people, category=category, created_by=created_by,
            ))
    if create_reserve:
        for start, stop in slots:
            duties.append(Duty.objects.create(
                event=event, title=reserve_title, day=day, start_time=start, end_time=stop,
                needed_people=needed_people, category=category, created_by=created_by,
            ))
    return duties


def todo_groups(event):
    from .models import PlanningItem

    groups = {key: [] for key, label in PlanningItem.TODO_PERIODS}
    unassigned = []
    for item in event.planning_items.filter(category="members"):
        groups.get(item.todo_period, unassigned).append(item)
    result = [(key, label, groups[key]) for key, label in PlanningItem.TODO_PERIODS]
    if unassigned:
        result.append(("", "Zeitpunkt offen", unassigned))
    return result
