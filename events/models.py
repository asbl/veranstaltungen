import math
import os

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from .attachments import attachment_path, validate_attachment, CONTENT_TYPES
from .list_settings import LISTS

User = settings.AUTH_USER_MODEL

ROLE_CHOICES = [
    ("member", "Mitglied"),
    ("guest", "Nichtmitglied"),
]

GAME_TYPE_CHOICES = [
    ("wish", "Wunsch"),
    ("bring", "Mitbringen"),
    ("both", "Wunsch & Mitbringen"),
]

WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


class Event(models.Model):
    list_settings = models.JSONField("Listeneinstellungen", default=list, blank=True)

    @property
    def visible_lists(self):
        from .list_settings import configured_lists
        attachments = {}
        if self.pk:
            for attachment in self.list_attachments.all():
                attachments.setdefault(attachment.list_key, []).append(attachment)
        return [{**row, "attachments": attachments.get(row["key"], [])}
                for row in configured_lists(self.list_settings)
                if row["enabled"] and (not row["planning"] or self.planning_enabled)]

    @property
    def responsibilities_show_status(self):
        from .list_settings import configured_lists
        return next(row["show_status"] for row in configured_lists(self.list_settings) if row["key"] == "orga")

    planning_enabled = models.BooleanField("Öffentliche Veranstaltung mit Vorab-Orga", default=False, db_default=False)
    title = models.CharField("Titel", max_length=200)
    description = models.TextField("Beschreibung", blank=True)
    location = models.CharField("Ort", max_length=200, blank=True)
    access_password_hash = models.CharField(
        "Zugangspasswort (gehasht)",
        max_length=128,
        blank=True,
        default="",
        help_text=(
            "Wird nur gehasht gespeichert (django.contrib.auth.hashers), nicht im Klartext. "
            "Leer lassen, um die Veranstaltung ohne Passwort zugänglich zu machen."
        ),
    )
    allow_known_persons_signup = models.BooleanField(
        "Bekannte Personen dürfen sich ohne Schlüssel anmelden",
        default=False,
        db_default=False,
        help_text=(
            "Bekannte Personen (Teilnahme an einer bereits beendeten Veranstaltung) dürfen "
            "die Veranstaltung ohne Passwort betreten: Auf der Zugangsseite können sie sich "
            "per E-Mail-Adresse einen Anmeldecode schicken lassen; mit angemeldetem Konto "
            "ist kein Zugangspasswort mehr nötig."
        ),
    )
    auto_readonly = models.BooleanField(
        "Nach Veranstaltungsende automatisch schreibschützen",
        default=False,
        db_default=False,
        help_text=(
            "Ist die Veranstaltung beendet (alle Tage liegen in der Vergangenheit), wird sie "
            "automatisch schreibgeschützt: alle Einträge bleiben einsehbar, Änderungen sind "
            "nur noch der Orga möglich."
        ),
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_events")
    editors = models.ManyToManyField(
        User, blank=True, related_name="editor_of_events", verbose_name="Orga"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Veranstaltung"
        verbose_name_plural = "Veranstaltungen"

    def __str__(self):
        return self.title

    def can_manage(self, user):
        """Admin (superuser) or Orga (creator/editors) — full edit rights on
        this event. Everyone else is a Teilnehmer, limited to their own
        entries."""
        if not user.is_authenticated:
            return False
        if user.is_superuser or self.created_by_id == user.pk:
            return True
        return self.editors.filter(pk=user.pk).exists()

    @property
    def has_access_password(self):
        return bool(self.access_password_hash)

    @property
    def is_finished(self):
        """Veranstaltung ist beendet: mindestens ein Tag und alle Tage liegen
        in der Vergangenheit. Ein Tag am heutigen Datum zählt erst nach seiner
        Endzeit als vorbei (ohne Endzeit erst ab dem Folgetag). Veranstaltungen
        ohne Tage (z. B. reine Planungsphasen) gelten nicht als beendet."""
        days = list(self.days.all())
        if not days:
            return False
        now = timezone.localtime()
        for day in days:
            if day.date > now.date():
                return False
            if day.date == now.date() and (day.end_time is None or day.end_time > now.time()):
                return False
        return True

    @property
    def is_readonly(self):
        """Schreibschutz für beendete Veranstaltungen (siehe auto_readonly).
        Das Betrachten bleibt weiterhin möglich - auch mit Zugangspasswort
        oder als bekannte Person."""
        return self.auto_readonly and self.is_finished

    def set_access_password(self, raw_password):
        self.access_password_hash = make_password(raw_password) if raw_password else ""

    def check_access_password(self, raw_password):
        if not self.access_password_hash:
            return False
        return check_password(raw_password, self.access_password_hash)


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    display_name = models.CharField("Anzeigename", max_length=200, blank=True)

    class Meta:
        verbose_name = "Profil"
        verbose_name_plural = "Profile"

    def __str__(self):
        return self.display_name or str(self.user)


def safe_get_or_create(model, defaults=None, **kwargs):
    """get_or_create that survives a race between concurrent duplicate requests
    (e.g. double-clicking a toggle button), which would otherwise raise
    IntegrityError on the unique_together constraint."""
    try:
        with transaction.atomic():
            return model.objects.get_or_create(defaults=defaults, **kwargs)
    except IntegrityError:
        return model.objects.get(**kwargs), False


def get_display_name(user):
    if not user.is_authenticated:
        return str(user)
    profile = getattr(user, "profile", None)
    if profile and profile.display_name:
        return profile.display_name
    return user.get_full_name() or user.username


def _finished_event_ids():
    """IDs der beendeten Veranstaltungen: mindestens ein Tag und alle Tage
    liegen in der Vergangenheit - ein Tag am heutigen Datum erst nach seiner
    Endzeit (gleiche Regel wie Event.is_finished, als Query für die Nutzung in
    is_known_person)."""
    now = timezone.localtime()
    today = now.date()
    open_day = (
        models.Q(days__date__gt=today)
        | models.Q(days__date=today, days__end_time__isnull=True)
        | models.Q(days__date=today, days__end_time__gt=now.time())
    )
    return (
        Event.objects.filter(days__isnull=False)
        .exclude(open_day)
        .distinct()
        .values("pk")
    )


def is_known_person(user):
    """Bekannte Person = hat an einer bereits beendeten Veranstaltung
    teilgenommen (Anmeldung über Attendance; mitgebrachte Personen ohne
    Konto zählen nicht, da sie kein Benutzerkonto haben). Für Veranstaltungen
    mit „Bekannte Personen dürfen sich ohne Schlüssel anmelden“ genügt damit
    ein Login per E-Mail-Code statt des Zugangspassworts."""
    if not getattr(user, "is_authenticated", False):
        return False
    return Attendance.objects.filter(user=user, event_id__in=_finished_event_ids()).exists()


class EventDay(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="days")
    date = models.DateField("Datum")
    start_time = models.TimeField("Beginn", null=True, blank=True)
    end_time = models.TimeField("Ende", null=True, blank=True)

    class Meta:
        ordering = ["date"]
        unique_together = ("event", "date")
        verbose_name = "Veranstaltungstag"
        verbose_name_plural = "Veranstaltungstage"

    def __str__(self):
        return f"{self.event.title} – {self.date}"

    @property
    def display_label(self):
        return f"{WEEKDAYS_DE[self.date.weekday()]}, {self.date.strftime('%d.%m.%Y')}"


class Attendance(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="attendances")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="attendances")
    name = models.CharField("Name", max_length=200, blank=True)
    role = models.CharField("Rolle", max_length=10, choices=ROLE_CHOICES, default="member")
    invited_by = models.CharField(
        "Eingeladen durch",
        max_length=200,
        blank=True,
        help_text="Welches Mitglied hat dich eingeladen?",
    )
    days = models.ManyToManyField(EventDay, related_name="attendances", verbose_name="Anwesende Tage")
    comment = models.CharField(
        "Kommentar",
        max_length=300,
        blank=True,
        help_text="Optional, z. B. „Ich bin nur nachmittags da, meine Gäste kommen erst abends“.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("event", "user")
        verbose_name = "Anwesenheit"
        verbose_name_plural = "Anwesenheiten"

    def __str__(self):
        return f"{self.name or self.user} @ {self.event} ({self.get_role_display()})"

    @property
    def display_name(self):
        profile = getattr(self.user, "profile", None)
        if profile and profile.display_name:
            return profile.display_name
        return self.name or get_display_name(self.user)


class BroughtPerson(models.Model):
    attendance = models.ForeignKey(Attendance, on_delete=models.CASCADE, related_name="brought_people")
    name = models.CharField("Name", max_length=200)
    role = models.CharField("Rolle", max_length=10, choices=ROLE_CHOICES, default="guest")
    days = models.ManyToManyField(EventDay, related_name="brought_people", verbose_name="Anwesende Tage")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Mitgebrachte Person"
        verbose_name_plural = "Mitgebrachte Personen"

    def __str__(self):
        return f"{self.name} (mitgebracht von {self.attendance.user})"


class EventVisit(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="visits")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="event_visits")
    last_visited_at = models.DateTimeField()

    class Meta:
        unique_together = ("event", "user")
        verbose_name = "Veranstaltungsbesuch"
        verbose_name_plural = "Veranstaltungsbesuche"

    def __str__(self):
        return f"{self.user} @ {self.event}: {self.last_visited_at}"


class Decline(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="declines")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="declines")
    comment = models.TextField("Kommentar", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("event", "user")
        ordering = ["-created_at"]
        verbose_name = "Absage"
        verbose_name_plural = "Absagen"

    def __str__(self):
        return f"{self.user} sagt ab für {self.event}"


class PotluckItem(models.Model):
    category = models.CharField("Liste", max_length=10, choices=[("bring", "Mitbringliste")], default="bring", db_default="bring")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="potluck_items")
    title = models.CharField("Titel", max_length=200)
    needed_quantity = models.PositiveIntegerField(
        "Soll-Menge", null=True, blank=True, help_text="Leer lassen, wenn keine feste Zielmenge nötig ist."
    )
    unit = models.CharField(
        "Einheit",
        max_length=50,
        blank=True,
        help_text="Optional, z. B. Flaschen, Liter oder Packungen.",
    )
    variable_quantity = models.PositiveIntegerField(
        "Sollmenge (variabel)",
        null=True,
        blank=True,
        help_text="Alternative zur festen Soll-Menge: X Stück je Y Anmeldungen.",
    )
    variable_per_persons = models.PositiveIntegerField(
        "pro X Anmeldungen",
        null=True,
        blank=True,
        help_text=(
            "Die Sollmenge steigt automatisch mit der Anzahl der Anmeldungen: "
            "„Sollmenge (variabel)“ Stück je angefangene X Personen."
        ),
    )
    comment = models.TextField("Kommentar", blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="potluck_items")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]
        verbose_name = "Mitbring-Item"
        verbose_name_plural = "Mitbringliste"

    def __str__(self):
        return self.title

    def format_quantity(self, quantity):
        """Format a quantity with the item's optional user-provided unit."""
        return f"{quantity} {self.unit}" if self.unit else str(quantity)

    def clean(self):
        super().clean()
        if self.needed_quantity is not None and (
            self.variable_quantity is not None or self.variable_per_persons is not None
        ):
            raise ValidationError(
                "Feste Soll-Menge und variable Sollmenge schließen sich gegenseitig aus."
            )
        if (self.variable_quantity is None) != (self.variable_per_persons is None):
            raise ValidationError(
                "Für eine variable Sollmenge müssen beide Felder ausgefüllt werden."
            )

    @property
    def is_variable(self):
        return self.variable_per_persons is not None and self.variable_quantity is not None

    @property
    def target_quantity(self):
        """The Soll-Menge to compare claims against. For variable items this
        scales with the event's attendee count (X je angefangene Y
        Personen). Reuses the `attendee_count` attribute event_detail's view
        precomputes on each item (no extra query); falls back to a live
        count when that isn't available (e.g. from the Django admin)."""
        if not self.is_variable:
            return self.needed_quantity
        attendee_count = getattr(self, "attendee_count", None)
        if attendee_count is None:
            attendee_count = (
                self.event.attendances.count()
                + BroughtPerson.objects.filter(attendance__event_id=self.event_id).count()
            )
        if attendee_count <= 0:
            return 0
        units = math.ceil(attendee_count / self.variable_per_persons)
        return units * self.variable_quantity

    @property
    def claimed_quantity(self):
        """Claims from people who are no longer attending don't count -
        otherwise an item looks "covered" while nobody will actually bring
        it. Reuses the `user_is_attending` flag event_detail's view already
        precomputes per claim (no extra query); falls back to a live check
        when that isn't available (e.g. from the Django admin)."""
        claims = list(self.claims.all())
        if not claims:
            return 0
        if self.event.planning_enabled:
            return sum(claim.quantity for claim in claims)
        if all(hasattr(c, "user_is_attending") for c in claims):
            return sum(claim.quantity for claim in claims if claim.user_is_attending)
        attending_ids = set(
            Attendance.objects.filter(
                event_id=self.event_id, user_id__in=[c.user_id for c in claims]
            ).values_list("user_id", flat=True)
        )
        # Zusagen ohne Konto hat die Orga eingetragen - sie zählen immer.
        attending_ids.add(None)
        return sum(claim.quantity for claim in claims if claim.user_id in attending_ids)

    @property
    def claimed_person_count(self):
        """Number of attending people who currently promise to bring this item."""
        claims = list(self.claims.all())
        if not claims:
            return 0
        if self.event.planning_enabled:
            return len(claims)
        if all(hasattr(claim, "user_is_attending") for claim in claims):
            return sum(1 for claim in claims if claim.user_is_attending)
        attending_ids = set(
            Attendance.objects.filter(
                event_id=self.event_id, user_id__in=[claim.user_id for claim in claims]
            ).values_list("user_id", flat=True)
        )
        attending_ids.add(None)
        return sum(1 for claim in claims if claim.user_id in attending_ids)

    @property
    def remaining(self):
        target = self.target_quantity
        if target is None:
            return None
        return max(target - self.claimed_quantity, 0)

    @property
    def is_missing(self):
        """Whether this item still needs someone to bring (more of) it."""
        target = self.target_quantity
        if target is None:
            return self.claimed_quantity == 0
        return self.remaining > 0


class PotluckClaim(models.Model):
    item = models.ForeignKey(PotluckItem, on_delete=models.CASCADE, related_name="claims")
    # Ohne Konto (user leer) trägt die Orga stattdessen einen freien Namen ein,
    # z. B. für Gäste oder Leute, die die Seite nicht selbst nutzen.
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name="potluck_claims"
    )
    name = models.CharField("Name (ohne Konto)", max_length=200, blank=True)
    quantity = models.PositiveIntegerField("Menge", default=1)
    comment = models.TextField("Kommentar", blank=True)
    # Gesetzt, wenn die Orga stellvertretend für jemand anderen eingetragen hat.
    entered_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Eingetragen von",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Claim"
        verbose_name_plural = "Claims"

    def __str__(self):
        return f"{self.person_label} übernimmt {self.item.format_quantity(self.quantity)} × {self.item}"

    @property
    def person_label(self):
        if self.user_id:
            return get_display_name(self.user)
        return self.name or "Unbekannt"


class Idea(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="ideas")
    text = models.TextField("Idee")
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="ideas")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Idee"
        verbose_name_plural = "Ideen"

    def __str__(self):
        return self.text[:50]


class IdeaComment(models.Model):
    idea = models.ForeignKey(Idea, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="idea_comments")
    text = models.TextField("Kommentar")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Ideen-Kommentar"
        verbose_name_plural = "Ideen-Kommentare"

    def __str__(self):
        return self.text[:50]


class IdeaCommentNotification(models.Model):
    """Queued email notification for a new IdeaComment, collected and sent as a
    batched digest by the send_idea_comment_notifications management command
    instead of immediately (see events/notifications.py)."""

    comment = models.ForeignKey(IdeaComment, on_delete=models.CASCADE, related_name="notifications")
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="idea_comment_notifications")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("comment", "recipient")
        verbose_name = "Ideen-Kommentar-Benachrichtigung"
        verbose_name_plural = "Ideen-Kommentar-Benachrichtigungen"

    def __str__(self):
        return f"Benachrichtigung an {self.recipient} für {self.comment}"


class IdeaLike(models.Model):
    idea = models.ForeignKey(Idea, on_delete=models.CASCADE, related_name="likes")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="idea_likes")

    class Meta:
        unique_together = ("idea", "user")
        verbose_name = "Idee-Like"
        verbose_name_plural = "Idee-Likes"

    def __str__(self):
        return f"{self.user} likes {self.idea}"


REACTION_EMOJI_CHOICES = [
    ("👍", "👍"),
    ("❤️", "❤️"),
    ("😄", "😄"),
    ("🎉", "🎉"),
    ("😮", "😮"),
    ("😢", "😢"),
]


class Question(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField("Frage")
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="questions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Frage"
        verbose_name_plural = "Fragen & Antworten"

    def __str__(self):
        return self.text[:50]


class QuestionLike(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="likes")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="question_likes")

    class Meta:
        unique_together = ("question", "user")
        verbose_name = "Frage-Like"
        verbose_name_plural = "Frage-Likes"

    def __str__(self):
        return f"{self.user} likes {self.question}"


class QuestionReaction(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="question_reactions")
    emoji = models.CharField("Emoji", max_length=8, choices=REACTION_EMOJI_CHOICES)

    class Meta:
        unique_together = ("question", "user", "emoji")
        verbose_name = "Frage-Reaktion"
        verbose_name_plural = "Frage-Reaktionen"

    def __str__(self):
        return f"{self.user} reagiert {self.emoji} auf {self.question}"


class Answer(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="answers")
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="replies"
    )
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="answers")
    text = models.TextField("Antwort")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Antwort"
        verbose_name_plural = "Antworten"

    def __str__(self):
        return self.text[:50]


class AnswerLike(models.Model):
    answer = models.ForeignKey(Answer, on_delete=models.CASCADE, related_name="likes")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="answer_likes")

    class Meta:
        unique_together = ("answer", "user")
        verbose_name = "Antwort-Like"
        verbose_name_plural = "Antwort-Likes"

    def __str__(self):
        return f"{self.user} likes {self.answer}"


class AnswerReaction(models.Model):
    answer = models.ForeignKey(Answer, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="answer_reactions")
    emoji = models.CharField("Emoji", max_length=8, choices=REACTION_EMOJI_CHOICES)

    class Meta:
        unique_together = ("answer", "user", "emoji")
        verbose_name = "Antwort-Reaktion"
        verbose_name_plural = "Antwort-Reaktionen"

    def __str__(self):
        return f"{self.user} reagiert {self.emoji} auf {self.answer}"


class Slot(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="slots")
    day = models.ForeignKey(EventDay, on_delete=models.CASCADE, related_name="slots")
    title = models.CharField("Titel", max_length=200)
    description = models.TextField("Beschreibung", blank=True)
    start_time = models.TimeField("Beginn")
    end_time = models.TimeField("Ende")
    max_participants = models.PositiveIntegerField("Max. Teilnehmerzahl")
    allow_waitlist = models.BooleanField(
        "Warteliste",
        default=False,
        help_text=(
            "Aktiviert: Es können sich auch nach Erreichen der Teilnehmerzahl weitere Personen "
            "anmelden (Warteliste). Deaktiviert: Bei Erreichen der Teilnehmerzahl ist keine "
            "weitere Anmeldung möglich."
        ),
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_slots")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["day__date", "start_time"]
        verbose_name = "Angebot/Workshop"
        verbose_name_plural = "Angebote/Workshops"

    def __str__(self):
        return f"{self.title} ({self.day.date} {self.start_time}–{self.end_time})"

    @property
    def signup_count(self):
        return self.signups.count()

    @property
    def is_full(self):
        return self.signup_count >= self.max_participants

    @property
    def spots_left(self):
        return max(self.max_participants - self.signup_count, 0)

    @property
    def waitlist_count(self):
        return max(self.signup_count - self.max_participants, 0)


class SlotSignup(models.Model):
    slot = models.ForeignKey(Slot, on_delete=models.CASCADE, related_name="signups")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="slot_signups")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("slot", "user")
        verbose_name = "Angebot/Workshop-Anmeldung"
        verbose_name_plural = "Angebot/Workshop-Anmeldungen"

    def __str__(self):
        return f"{self.user} @ {self.slot}"


class SlotComment(models.Model):
    slot = models.ForeignKey(Slot, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="slot_comments")
    text = models.TextField("Kommentar")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Angebot/Workshop-Kommentar"
        verbose_name_plural = "Angebot/Workshop-Kommentare"

    def __str__(self):
        return self.text[:50]


class BggTranslation(models.Model):
    """One durable translation per BGG ID, independent of event lifetimes."""

    bgg_id = models.PositiveIntegerField(primary_key=True)
    source_text = models.TextField(blank=True)
    text_de = models.TextField("Deutsche Beschreibung", blank=True)
    status = models.CharField(
        max_length=10,
        choices=[("pending", "Wird übersetzt"), ("ready", "Gespeichert"), ("failed", "Fehlgeschlagen")],
        default="ready",
    )
    attempted_at = models.DateTimeField(null=True, blank=True)
    translated_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    revision = models.PositiveIntegerField(default=0)
    error_message = models.CharField(max_length=300, blank=True)

    @property
    def is_running(self):
        # A worker may have died after sending its request. Never resend it;
        # after a short grace period, allow manual recovery instead.
        return self.status == "pending" and self.attempted_at and (
            timezone.now() - self.attempted_at
        ).total_seconds() < 90

    @property
    def attribution(self):
        if self.edited_at:
            return "Deutsche Beschreibung (bearbeitet)"
        return "Automatisch mit DeepL übersetzt"


class GameEntry(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="games")
    name = models.CharField("Spielname", max_length=200)
    type = models.CharField("Typ", max_length=10, choices=GAME_TYPE_CHOICES, default="bring")
    description = models.CharField("Kurzbeschreibung", max_length=300, blank=True)
    summary = models.TextField(
        "Kurzfassung",
        blank=True,
        db_default="",
        help_text="Wenige Sätze zum Spielablauf. Ersetzt in der Druckansicht die lange BGG-Beschreibung.",
    )
    expansions = models.TextField(
        "Erweiterungen",
        blank=True,
        db_default="",
        help_text="Welche Erweiterungen bringst du mit? (z. B. „Seefahrer“)",
    )
    bgg_id = models.PositiveIntegerField("BoardGameGeek-ID", null=True, blank=True)
    min_players = models.PositiveSmallIntegerField("Min. Spieler", null=True, blank=True)
    max_players = models.PositiveSmallIntegerField("Max. Spieler", null=True, blank=True)
    playtime_minutes = models.PositiveSmallIntegerField("Spieldauer (Min.)", null=True, blank=True)
    min_age = models.PositiveSmallIntegerField("Mindestalter", null=True, blank=True)
    bgg_year = models.PositiveSmallIntegerField("Erscheinungsjahr", null=True, blank=True)
    complexity = models.DecimalField("Komplexität", max_digits=3, decimal_places=2, null=True, blank=True)
    bgg_rating = models.DecimalField("BGG-Wertung", max_digits=4, decimal_places=2, null=True, blank=True)
    # Keep database defaults for backwards-compatible deployments: migrations
    # run before Gunicorn starts, so workers from the previous release can
    # briefly keep inserting rows without these newer columns.
    bgg_description = models.TextField("BGG-Beschreibung", blank=True, db_default="")
    bgg_categories = models.JSONField("BGG-Kategorien", default=list, blank=True, db_default=[])
    bgg_mechanics = models.JSONField("BGG-Mechanismen", default=list, blank=True, db_default=[])
    thumbnail_url = models.URLField("Vorschaubild", blank=True)
    bgg_synced_at = models.DateTimeField("BGG zuletzt synchronisiert", null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="game_entries")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("event", "name")
        verbose_name = "Brettspiel-Eintrag"
        verbose_name_plural = "Brettspielliste"

    def __str__(self):
        return self.name

    @property
    def complexity_label(self):
        """Übersetzt den BGG-Weight-Wert (1–5) in eine kurze Stufe für die Liste."""
        if self.complexity is None:
            return ""
        if self.complexity < 2:
            return "einfach"
        if self.complexity < 3.5:
            return "mittel"
        return "komplex"

    @property
    def players_label(self):
        """Kompakte Spielerzahl für die Liste, z. B. „3–4“ oder „ab 2“."""
        if self.min_players and self.max_players and self.min_players != self.max_players:
            return f"{self.min_players}–{self.max_players}"
        if self.min_players:
            return f"ab {self.min_players}"
        if self.max_players:
            return f"bis {self.max_players}"
        return ""


class GameBringClaim(models.Model):
    game = models.ForeignKey(GameEntry, on_delete=models.CASCADE, related_name="bring_claims")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="game_bring_claims")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("game", "user")
        verbose_name = "Mitbring-Zusage (Spiel)"
        verbose_name_plural = "Mitbring-Zusagen (Spiele)"

    def __str__(self):
        return f"{self.user} bringt {self.game} mit"


class GameExplainClaim(models.Model):
    game = models.ForeignKey(GameEntry, on_delete=models.CASCADE, related_name="explain_claims")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="game_explain_claims")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("game", "user")
        verbose_name = "Erklär-Zusage (Spiel)"
        verbose_name_plural = "Erklär-Zusagen (Spiele)"

    def __str__(self):
        return f"{self.user} erklärt {self.game}"


class GameComment(models.Model):
    game = models.ForeignKey(GameEntry, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="game_comments")
    text = models.TextField("Kommentar")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Spiel-Kommentar"
        verbose_name_plural = "Spiel-Kommentare"

    def __str__(self):
        return self.text[:50]


class GameInterest(models.Model):
    game = models.ForeignKey(GameEntry, on_delete=models.CASCADE, related_name="interested_users")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="game_interests")

    class Meta:
        unique_together = ("game", "user")
        verbose_name = "Spiel-Interesse"
        verbose_name_plural = "Spiel-Interessen"

    def __str__(self):
        return f"{self.user} interessiert an {self.game}"


class Duty(models.Model):
    category = models.CharField("Bereich", max_length=10, choices=[("service", "Dienstplan"), ("setup", "Aufbau"), ("teardown", "Abbau")], default="service", db_default="service")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="duties")
    title = models.CharField("Aufgabe", max_length=200)
    description = models.TextField("Beschreibung", blank=True)
    day = models.ForeignKey(
        EventDay, on_delete=models.SET_NULL, null=True, blank=True, related_name="duties", verbose_name="Tag"
    )
    start_time = models.TimeField("Beginn", null=True, blank=True)
    end_time = models.TimeField("Ende", null=True, blank=True)
    needed_people = models.PositiveIntegerField(
        "Benötigte Personen", null=True, blank=True, help_text="Leer lassen, wenn keine feste Zielzahl nötig ist."
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_duties")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]
        verbose_name = "Dienst"
        verbose_name_plural = "Dienste & Orga"

    def __str__(self):
        return self.title

    @property
    def signup_groups(self):
        signups = list(self.signups.all())
        groups = [("Besetzung", [s for s in signups if not s.is_reserve])]
        # Springer gibt es nur im Dienstplan; Auf- und Abbau werden regulär besetzt.
        if self.category == "service":
            groups.append(("Springer", [s for s in signups if s.is_reserve]))
        return groups

    @property
    def signup_count(self):
        return sum(not signup.is_reserve for signup in self.signups.all())

    @property
    def is_full(self):
        if self.needed_people is None:
            return False
        return self.signup_count >= self.needed_people

    @property
    def spots_left(self):
        if self.needed_people is None:
            return None
        return max(self.needed_people - self.signup_count, 0)


class DutySignup(models.Model):
    is_reserve = models.BooleanField("Springer", default=False, db_default=False)
    duty = models.ForeignKey(Duty, on_delete=models.CASCADE, related_name="signups")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="duty_signups")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("duty", "user")
        verbose_name = "Dienst-Anmeldung"
        verbose_name_plural = "Dienst-Anmeldungen"

    def __str__(self):
        return f"{self.user} @ {self.duty}"


class OrgaActionLog(models.Model):
    """Protokoll für Planungsänderungen und stellvertretende Orga-Aktionen."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="orga_action_logs")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="orga_actions")
    description = models.CharField("Beschreibung", max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Orga-Aktion (Log)"
        verbose_name_plural = "Orga-Aktionen (Log)"

    def __str__(self):
        return f"{self.created_at:%d.%m.%Y %H:%M} – {self.actor}: {self.description}"


class PlanningItem(models.Model):
    everyone = models.BooleanField("Alle können mitmachen", default=False, db_default=False, help_text="Verantwortlich sind alle. Jede Person kann ihren Namen unter „Schon gemacht“ ergänzen.")
    completed_by = models.ManyToManyField(User, blank=True, related_name="completed_advert_items", verbose_name="Schon gemacht")
    revision = models.PositiveIntegerField(default=0, db_default=0, editable=False)
    due_date = models.DateField("Fällig am", null=True, blank=True)
    published_on = models.DateField("Veröffentlicht am", null=True, blank=True)
    publication_url = models.URLField("Link zur Veröffentlichung", blank=True, default="", db_default="")
    template_source = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="template_copies", editable=False,
    )
    TODO_PERIODS = [
        ("year", "Ein Jahr vorher"),
        ("6_8", "6–8 Wochen vorher"),
        ("4_5", "4–5 Wochen vorher"),
        ("2_3", "2–3 Wochen vorher"),
        ("short", "Kurz vor der Veranstaltung"),
    ]
    todo_period = models.CharField("Zeitgruppe", max_length=10, choices=TODO_PERIODS, blank=True, default="", db_default="")

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="planning_items")
    category = models.CharField("Liste", max_length=10, choices=[("orga", "Verantwortlichkeiten-Liste"), ("advert", "Werbung"), ("members", "TODO-Liste")], default="orga")
    title = models.CharField("Item / Wo wird Werbung gemacht?", max_length=200)
    status = models.CharField("Status", max_length=12, choices=[("open", "Offen"), ("progress", "In Arbeit"), ("waiting", "Wartet"), ("done", "Erledigt")], default="open")
    people = models.CharField("Person(en)", max_length=300, blank=True, help_text="Auch Personen ohne Benutzerkonto können genannt werden.")
    last_done_by = models.CharField(
        "Wer hat es das letzte Mal gemacht",
        max_length=300,
        blank=True,
        help_text="Orientierung für die Zuteilung: wer die Aufgabe bei der letzten zugehörigen Veranstaltung übernommen hat.",
    )
    notes = models.TextField("Notizen", blank=True)
    weeks_before = models.PositiveSmallIntegerField("Wochen vorher", null=True, blank=True, help_text="Vorlauf vor der Veranstaltung; leer lassen, solange der Zeitpunkt offen ist.")
    reference_url = models.URLField("Link", blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["event", "template_source"], name="unique_planning_template_copy")]
        verbose_name = "Planungsaufgabe"
        verbose_name_plural = "Planungsaufgaben"

    def has_responsible_user(self, user):
        import re
        if not user.is_authenticated:
            return False
        name = get_display_name(user).strip()
        return bool(name and re.search(r"(?:^|[,;\n])\s*" + re.escape(name) + r"\s*(?:$|[,;\n])", self.people, re.IGNORECASE))

    @property
    def participation_count(self):
        return len(self.completed_by.all())

    @property
    def planning_status(self):
        if self.everyone:
            return "progress" if self.participation_count else "open"
        return self.status

    def __str__(self):
        return self.title


class ListAttachment(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="list_attachments")
    list_key = models.CharField("Liste", max_length=20, choices=[(key, label) for key, label, _ in LISTS])
    file = models.FileField("Datei", upload_to=attachment_path, validators=[validate_attachment])
    original_name = models.CharField("Dateiname", max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    @property
    def content_type(self):
        from pathlib import Path
        return CONTENT_TYPES.get(Path(self.file.name).suffix.lower(), "application/octet-stream")

    @property
    def is_image(self):
        return self.content_type.startswith("image/")

    def __str__(self):
        return self.original_name


class AdvertIdentity(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="advert_identities")
    name = models.CharField("Anzeigename", max_length=200)
    aliases = models.TextField("Weitere Namen / Aliase", blank=True, max_length=2000)

    class Meta:
        ordering = ["name", "pk"]


def default_site_name():
    """Name des Betreibers (Verein/Anbieter) aus der Umgebung, neutraler Fallback."""
    return (
        os.environ.get("SITE_NAME")
        or os.environ.get("ORGANIZATION_NAME")
        or "Veranstaltungen"
    ).strip() or "Veranstaltungen"


class SiteSettings(models.Model):
    """Branding des Betreibers (Name, Logo) als Singleton (immer pk=1).

    Wird über den Admin gepflegt (oder per Umgebungsvariable SITE_NAME
    vorbelegt) und liegt damit in der Datenbank bzw. als Datei unter
    MEDIA_ROOT - nie fest im Code. So kann derselbe Code von beliebigen
    Anbietern selbst gehostet werden.
    """

    name = models.CharField("Name (Verein/Anbieter)", max_length=200, default=default_site_name)
    logo = models.FileField("Logo", upload_to="site/", blank=True, default="")
    updated_at = models.DateTimeField("Zuletzt geändert", auto_now=True)

    class Meta:
        verbose_name = "Betreiber-Einstellungen"
        verbose_name_plural = "Betreiber-Einstellungen"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={"name": default_site_name()})
        return obj

    def __str__(self):
        return self.name
