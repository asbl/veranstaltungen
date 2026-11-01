from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory
from django.utils import timezone

from .attachments import AttachmentField, validate_attachment
from . import bgg
from .models import (
    Answer,
    Attendance,
    BroughtPerson,
    Decline,
    Duty,
    Event,
    EventDay,
    ListAttachment,
    GAME_TYPE_CHOICES,
    GameEntry,
    GameComment,
    Idea,
    IdeaComment,
    PlanningItem,
    PotluckClaim,
    PotluckItem,
    Question,
    Slot,
    SlotComment,
    get_display_name,
)


class UserDisplayChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        name = get_display_name(obj)
        return name if name == obj.username else f"{name} ({obj.username})"


class EventForm(forms.ModelForm):
    list_order = forms.CharField(required=False, widget=forms.HiddenInput)
    lists_configured = forms.BooleanField(required=False, widget=forms.HiddenInput, initial=True)

    def clean_list_order(self):
        from .list_settings import LISTS
        value = self.cleaned_data["list_order"]
        if not self.data.get(self.add_prefix("lists_configured")):
            return value
        keys = value.split(",")
        if len(keys) != len(LISTS) or set(keys) != {key for key, _, _ in LISTS}:
            raise ValidationError("Ungültige Listenreihenfolge. Bitte die Seite neu laden.")
        return value

    # Kein Model-Feld: der Klartext wird nie gespeichert, sondern in save()
    # sofort gehasht (Event.set_access_password). Leer lassen behält beim
    # Bearbeiten das bestehende Passwort bei, statt es zu löschen.
    access_password = forms.CharField(
        label="Zugangspasswort",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=(
            "Muss beim ersten Aufruf der Veranstaltung einmalig eingegeben werden. "
            "Leer lassen für keinen Zugangsschutz. Beim Bearbeiten: leer lassen, um "
            "das bestehende Passwort unverändert zu lassen."
        ),
    )

    class Meta:
        model = Event
        fields = ["title", "description", "location", "planning_enabled", "allow_known_persons_signup", "auto_readonly", "feedback_url"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .list_settings import configured_lists
        rows = list(configured_lists(self.instance.list_settings))
        self.fields["list_order"].initial = ",".join(row["key"] for row in rows)
        if self.is_bound:
            order = self.data.get(self.add_prefix("list_order"), "").split(",")
            rows.sort(key=lambda row: order.index(row["key"]) if row["key"] in order else len(order))
        self.list_rows = []
        attachments = list(self.instance.list_attachments.all()) if self.instance.pk else []
        for row in rows:
            key = row["key"]
            enabled = f"list_{key}_enabled"
            description = f"list_{key}_description"
            self.fields[enabled] = forms.BooleanField(label=row["label"], required=False, initial=row["enabled"])
            self.fields[description] = forms.CharField(label="Beschreibung", required=False, initial=row["description"], max_length=10000, widget=forms.Textarea(attrs={"rows": 3}))
            status_field = None
            if key == "orga":
                status_name = f"list_{key}_show_status"
                self.fields[status_name] = forms.BooleanField(label="Status anzeigen", required=False, initial=row["show_status"])
                status_field = self[status_name]
            uploads = f"list_{key}_uploads"
            removals = f"list_{key}_remove"
            self.fields[uploads] = AttachmentField(label="Bilder und PDFs hinzufügen", required=False,
                validators=[validate_attachment], widget=AttachmentField.widget(attrs={"accept": ".png,.jpg,.jpeg,.gif,.webp,.pdf"}))
            existing = [file for file in attachments if file.list_key == key]
            self.fields[removals] = forms.MultipleChoiceField(label="Anhänge entfernen", required=False,
                choices=[(str(file.pk), file.original_name) for file in existing], widget=forms.CheckboxSelectMultiple)
            self.list_rows.append({**row, "enabled_field": self[enabled], "description_field": self[description],
                "status_field": status_field, "uploads_field": self[uploads], "removals_field": self[removals], "attachments": existing})
        if self.instance.pk and self.instance.has_access_password:
            self.fields["access_password"].help_text = (
                "Es ist bereits ein Passwort gesetzt. Leer lassen, um es beizubehalten, "
                "oder ein neues eingeben, um es zu ersetzen."
            )

    def save(self, commit=True):
        event = super().save(commit=False)
        if self.cleaned_data.get("lists_configured"):
            event.list_settings = [
                {"key": key, "enabled": self.cleaned_data[f"list_{key}_enabled"],
                 "description": self.cleaned_data[f"list_{key}_description"],
                 **({"show_status": self.cleaned_data["list_orga_show_status"]} if key == "orga" else {})}
                for key in self.cleaned_data["list_order"].split(",")
            ]
        new_password = self.cleaned_data.get("access_password")
        if new_password:
            event.set_access_password(new_password)
        if commit:
            event.save()
            self.save_m2m()
        return event

    def save_attachments(self):
        """Call after the event and all formsets have successfully saved."""
        from pathlib import Path
        for row in self.list_rows:
            key = row["key"]
            self.instance.list_attachments.filter(pk__in=self.cleaned_data[f"list_{key}_remove"]).delete()
            for upload in self.cleaned_data[f"list_{key}_uploads"]:
                ListAttachment.objects.create(event=self.instance, list_key=key, file=upload,
                    original_name=Path(upload.name).name)


class EventOrgaForm(forms.ModelForm):
    editors = UserDisplayChoiceField(
        queryset=None,
        required=False,
        label="Orga",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "orga-editor-list"}),
        help_text="Diese Personen können die Veranstaltung genauso bearbeiten wie du.",
    )

    class Meta:
        model = Event
        fields = ["editors"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["editors"].queryset = get_user_model().objects.order_by("username")


class EventDayForm(forms.ModelForm):
    class Meta:
        model = EventDay
        fields = ["date", "start_time", "end_time"]
        widgets = {
            # format explizit setzen, da sonst das lokalisierte Format
            # (de-de: TT.MM.JJJJ) verwendet wird, das der Browser für
            # <input type="date"> nicht erkennt und das Feld leer anzeigt,
            # obwohl ein Wert gesetzt ist.
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }


EventDayFormSet = inlineformset_factory(
    Event, EventDay, form=EventDayForm, extra=1, can_delete=True, min_num=1, validate_min=True
)


class DayChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.display_label


class DaySingleChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.display_label


class AttendanceForm(forms.ModelForm):
    days = DayChoiceField(
        queryset=EventDay.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label="Anwesende Tage",
    )

    class Meta:
        model = Attendance
        # Der Name des angemeldeten Accounts kommt zentral aus UserProfile.
        # Attendance.name bleibt vorerst als Legacy-Feld erhalten, damit alte
        # Datensätze und Migrationen kompatibel bleiben, ist aber nicht mehr
        # parallel im Veranstaltungsformular editierbar.
        fields = ["role", "days", "invited_by", "comment"]
        widgets = {
            "role": forms.RadioSelect(attrs={"class": "role-select"}),
            "invited_by": forms.TextInput(attrs={"placeholder": "Name des Mitglieds", "class": "invited-by-field"}),
            "comment": forms.TextInput(attrs={"placeholder": "z. B. Ich bin nur nachmittags da…"}),
        }
        labels = {"invited_by": "Eingeladen durch", "comment": "Kommentar"}

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is not None:
            self.fields["days"].queryset = event.days.all()


class BroughtPersonForm(forms.ModelForm):
    days = DayChoiceField(
        queryset=EventDay.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label="Anwesende Tage",
        required=True,
        error_messages={"required": "Bitte wähle mindestens einen Anwesenheitstag aus."},
    )

    class Meta:
        model = BroughtPerson
        fields = ["name", "role", "days"]
        widgets = {"role": forms.RadioSelect}

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is not None:
            self.fields["days"].queryset = event.days.all()


BroughtPersonFormSet = inlineformset_factory(
    Attendance, BroughtPerson, form=BroughtPersonForm, extra=1, can_delete=True
)


class DeclineForm(forms.ModelForm):
    class Meta:
        model = Decline
        fields = ["comment"]
        widgets = {"comment": forms.Textarea(attrs={"rows": 3})}
        labels = {"comment": "Grund (optional)"}


class PotluckItemForm(forms.ModelForm):
    class Meta:
        model = PotluckItem
        fields = ["title", "needed_quantity", "unit", "variable_quantity", "variable_per_persons", "comment"]


class PotluckClaimForm(forms.ModelForm):
    class Meta:
        model = PotluckClaim
        fields = ["quantity", "comment"]


class PersonChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return get_display_name(obj)


class PotluckOrgaClaimForm(forms.ModelForm):
    """Orga trägt ein, dass jemand anderes etwas mitbringt - entweder eine
    Person mit Konto oder (z. B. Gäste) einfach per Namen."""

    user = PersonChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        label="Person",
        empty_label="– Person ohne Konto (Name unten eintragen) –",
    )

    class Meta:
        model = PotluckClaim
        fields = ["user", "name", "quantity", "comment"]
        widgets = {"name": forms.TextInput(attrs={"list": "potluck-person-names", "autocomplete": "off"})}
        help_texts = {"name": "Nur nötig, wenn die Person oben nicht auswählbar ist, z. B. Gäste."}

    def __init__(self, *args, people=None, **kwargs):
        super().__init__(*args, **kwargs)
        people = list(people or [])
        people.sort(key=lambda u: get_display_name(u).lower())
        self.fields["user"].queryset = get_user_model().objects.filter(pk__in=[u.pk for u in people])
        # Sortierung nach Anzeigename statt nach Benutzername beibehalten.
        self.fields["user"].choices = [("", self.fields["user"].empty_label)] + [
            (u.pk, get_display_name(u)) for u in people
        ]

    def clean(self):
        cleaned = super().clean()
        user = cleaned.get("user")
        name = (cleaned.get("name") or "").strip()
        if user and name:
            self.add_error("name", "Bitte entweder eine Person auswählen oder einen Namen eintragen, nicht beides.")
        elif not user and not name:
            self.add_error("user", "Bitte eine Person auswählen oder unten einen Namen eintragen.")
        cleaned["name"] = name
        return cleaned


class IdeaForm(forms.ModelForm):
    class Meta:
        model = Idea
        fields = ["text"]


class IdeaCommentForm(forms.ModelForm):
    class Meta:
        model = IdeaComment
        fields = ["text"]


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ["text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 3})}
        labels = {"text": "Frage"}


class AnswerForm(forms.ModelForm):
    class Meta:
        model = Answer
        fields = ["text"]
        labels = {"text": "Antwort"}


class SlotForm(forms.ModelForm):
    day = DaySingleChoiceField(queryset=EventDay.objects.none(), label="Tag")

    class Meta:
        model = Slot
        fields = ["day", "title", "description", "start_time", "end_time", "max_participants", "allow_waitlist"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }
        labels = {"title": "Was soll angeboten werden?"}

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is not None:
            self.fields["day"].queryset = event.days.all()

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        if start_time and end_time and start_time >= end_time:
            raise ValidationError("Das Ende muss nach dem Beginn liegen.")
        return cleaned_data


class SlotCommentForm(forms.ModelForm):
    class Meta:
        model = SlotComment
        fields = ["text"]


class DutyForm(forms.ModelForm):
    category = forms.ChoiceField(choices=Duty._meta.get_field("category").choices, required=False, initial="service", label="Bereich")

    def clean_category(self):
        return self.cleaned_data.get("category") or self.instance.category or "service"

    day = DaySingleChoiceField(
        queryset=EventDay.objects.none(), label="Tag", required=False, empty_label="Kein bestimmter Tag"
    )

    class Meta:
        model = Duty
        fields = ["category", "title", "description", "day", "start_time", "end_time", "needed_people"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }
        labels = {"title": "Aufgabe"}

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is not None:
            self.fields["day"].queryset = event.days.all()

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        if start_time and end_time and start_time >= end_time:
            raise ValidationError("Das Ende muss nach dem Beginn liegen.")
        return cleaned_data


class GameEntryForm(forms.ModelForm):
    # Kein Modell-Feld: die Kategorien liegen als Liste im Modell, im Formular
    # werden sie als kommagetrennter Text gepflegt.
    bgg_categories_text = forms.CharField(
        label="Kategorien (BGG)",
        required=False,
        help_text="Kommagetrennt, z. B. „Strategiespiel, Würfelspiel“.",
    )

    class Meta:
        model = GameEntry
        fields = [
            "name",
            "type",
            "description",
            "summary",
            "expansions",
            "bgg_id",
            "min_players",
            "max_players",
            "bgg_rating",
            "complexity",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2}), "summary": forms.Textarea(attrs={"rows": 4})}
        labels = {
            "complexity": "Weight (Komplexität, 1–5)",
            "bgg_rating": "BGG-Wertung (1–10)",
        }
        help_texts = {
            "bgg_id": "Die Zahl aus dem BGG-Link, z. B. 13 aus boardgamegeek.com/boardgame/13/catan.",
        }

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event or getattr(self.instance, "event", None)
        self._initial_bgg_id = self.instance.bgg_id if self.instance.pk else None
        self.bgg_sync_error = ""
        self.fields["expansions"].widget.attrs["rows"] = 2
        if self.instance.pk:
            self.fields["bgg_categories_text"].initial = ", ".join(self.instance.bgg_categories)
        else:
            # Nur beim Anlegen: später geht das über „Ich kann das Spiel erklären“ in der Liste.
            self.fields["can_explain"] = forms.BooleanField(
                label="Ich kann das Spiel auch erklären (Erklärbär)",
                required=False,
            )

    def save(self, commit=True):
        game = super().save(commit=False)
        game.bgg_categories = [
            part.strip() for part in self.cleaned_data.get("bgg_categories_text", "").split(",") if part.strip()
        ]
        if game.bgg_id != self._initial_bgg_id:
            game.bgg_synced_at = None
        self._sync_bgg_data(game)
        if commit:
            game.save()
            self.save_m2m()
        return game

    def _sync_bgg_data(self, game):
        """Holt die Spieldaten spätestens beim Speichern von BGG nach, damit
        die Felder auch dann befüllt sind, wenn die JavaScript-Vorschläge im
        Browser nicht gegriffen haben. Fehler blockieren das Speichern nicht."""
        if not game.bgg_id:
            return
        fields_complete = all(
            [game.min_players, game.max_players, game.bgg_rating, game.complexity, game.bgg_categories]
        )
        if game.bgg_synced_at and fields_complete:
            return
        try:
            data = bgg.fetch_bgg_game(game.bgg_id)
        except (bgg.BggLookupError, ValueError) as exc:
            self.bgg_sync_error = f"BGG-Daten konnten beim Speichern nicht geladen werden ({exc})."
            return
        game.min_players = data["min_players"]
        game.max_players = data["max_players"]
        game.playtime_minutes = data["playtime_minutes"]
        game.min_age = data["min_age"]
        game.bgg_year = data["year"]
        game.complexity = data["complexity"]
        game.bgg_rating = data["rating"]
        game.bgg_description = data["description"]
        game.bgg_categories = data["categories"]
        game.bgg_mechanics = data["mechanics"]
        game.thumbnail_url = data["thumbnail_url"]
        game.bgg_synced_at = timezone.now()

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.event is not None:
            qs = GameEntry.objects.filter(event=self.event, name=name)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError("Ein Spiel mit diesem Namen ist für diese Veranstaltung bereits eingetragen.")
        return name


class GameBulkForm(forms.Form):
    """Mehrere Spiele auf einmal: Jede Zeile besteht aus einem Titel und
    optional einer BGG-ID. Die Zeilen kommen als gleich lange Listen
    ("name"/"bgg_id") aus dem Formular, leere Zeilen werden ignoriert."""

    MAX_ROWS = 40
    INITIAL_ROWS = 5

    type = forms.ChoiceField(label="Typ", choices=GAME_TYPE_CHOICES, initial="bring")

    def __init__(self, data=None, *, event, **kwargs):
        super().__init__(data, **kwargs)
        self.event = event
        self.rows = []
        self.row_errors = []
        if data is not None:
            names = data.getlist("name")
            bgg_ids = data.getlist("bgg_id")
            # Angehakte Kästchen werden nur mit ihrer Zeilennummer übertragen.
            explain = set(data.getlist("explain"))
            for index, name in enumerate(names):
                bgg_id = bgg_ids[index].strip() if index < len(bgg_ids) else ""
                self.rows.append({"name": " ".join(name.split()), "bgg_id": bgg_id, "explain": str(index) in explain})
        while len(self.rows) < self.INITIAL_ROWS:
            self.rows.append({"name": "", "bgg_id": "", "explain": False})

    def clean(self):
        cleaned = super().clean()
        max_length = GameEntry._meta.get_field("name").max_length
        existing = {
            name.casefold(): pk
            for pk, name in GameEntry.objects.filter(event=self.event).values_list("pk", "name")
        }
        seen = set()
        entries = []
        for row in self.rows:
            name = row["name"]
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            if len(name) > max_length:
                self.row_errors.append(f"„{name[:40]}…“ ist zu lang (höchstens {max_length} Zeichen).")
                continue
            bgg_id = None
            if row["bgg_id"]:
                if not row["bgg_id"].isdigit() or int(row["bgg_id"]) < 1:
                    self.row_errors.append(f"Die BGG-ID bei „{name}“ ist keine gültige Zahl.")
                    continue
                bgg_id = int(row["bgg_id"])
            entries.append({"name": name, "bgg_id": bgg_id, "explain": row["explain"], "existing_pk": existing.get(key)})
        if len(entries) > self.MAX_ROWS:
            self.row_errors.append(f"Bitte höchstens {self.MAX_ROWS} Spiele auf einmal eintragen.")
        if not entries and not self.row_errors:
            self.row_errors.append("Bitte mindestens einen Spieltitel eintragen.")
        if self.row_errors:
            raise ValidationError(self.row_errors)
        cleaned["entries"] = entries
        return cleaned


class BggTranslationForm(forms.Form):
    bgg_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    text_de = forms.CharField(
        label="Deutsche Beschreibung",
        widget=forms.Textarea(attrs={"rows": 16}),
        help_text="Absätze bleiben erhalten. Änderungen gelten für dieses Spiel in allen Veranstaltungen.",
    )


class GameCommentForm(forms.ModelForm):
    class Meta:
        model = GameComment
        fields = ["text"]


class PlanningItemForm(forms.ModelForm):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput, required=False)
    PRESENTATION = {
        "orga": {
            "name": "Verantwortlichkeit",
            "labels": {"title": "Verantwortlichkeit", "notes": "Hinweise zur Organisation", "reference_url": "Link zur Aufgabe"},
            "example": "z. B. Schilder erstellen oder Material besorgen",
        },
        "advert": {
            "name": "Werbemaßnahme",
            "labels": {"title": "Wo wird Werbung gemacht?", "people": "Verantwortlich für die Werbung", "notes": "Hinweise zur Werbung", "last_done_by": "Wer hat hier zuletzt Werbung gemacht?", "reference_url": "Link"},
            "example": "z. B. Lokalzeitung, im eigenen Signal-Status oder einen Flyer ins Auto hängen",
        },
        "members": {
            "name": "TODO-Aufgabe",
            "labels": {"title": "Aufgabe / Anfrage / Reminder"},
            "example": "z. B. Mitglieder an Kuchenspenden erinnern",
        },
    }

    def __init__(self, *args, category="orga", show_status=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["revision"].required = bool(self.instance.pk)
        self.initial["revision"] = self.instance.revision
        if category == "orga" and not show_status:
            del self.fields["status"]
        self.category = category
        self.presentation = self.PRESENTATION[category]
        self.fields["category"].widget = forms.HiddenInput()
        self.fields["category"].disabled = True
        self.initial["category"] = category
        if category != "members":
            del self.fields["todo_period"]
        if category != "advert":
            for name in ("due_date", "published_on", "publication_url", "everyone"):
                del self.fields[name]
        for name, label in self.presentation["labels"].items():
            self.fields[name].label = label
        self.fields["title"].help_text = self.presentation["example"]
        if category == "advert":
            del self.fields["publication_url"]
            self.initial["reference_url"] = self.instance.reference_url or self.instance.publication_url
            self.fields["reference_url"].help_text = "Link zur Veröffentlichung oder zum Werbekanal."
            self.fields["published_on"].label = "Veröffentlicht am"
            self.fields["people"].help_text = "Bei „Alle können mitmachen“ wird automatisch Alle angezeigt; dieses Feld bleibt leer."
            self.fields["status"].help_text = "Aktionen für alle bleiben offen, damit weitere Personen mitmachen können."
            self.fields["last_done_by"].help_text = "Wer diesen Werbekanal bei der letzten Veranstaltung betreut hat."

    @property
    def basic_fields(self):
        advanced = {field.name for field in self.advanced_fields}
        return [field for field in self if not field.is_hidden and field.name not in advanced]

    @property
    def advanced_fields(self):
        names = ["due_date", "published_on", "reference_url", "last_done_by"] if self.category == "advert" else ["last_done_by", "reference_url"]
        return [self[name] for name in names if name in self.fields]

    @property
    def advanced_has_errors(self):
        return any(field.errors for field in self.advanced_fields)

    def save(self, commit=True):
        # Legacy publication links populate the single editable link. Preserve a
        # second, distinct legacy URL until the organizer explicitly handles it.
        if self.category == "advert" and (not self.instance.reference_url or self.instance.publication_url == self.instance.reference_url or self.instance.publication_url == self.initial.get("reference_url")):
            self.instance.publication_url = ""
        return super().save(commit=commit)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("everyone"):
            cleaned["people"] = ""
            cleaned["status"] = "open"
        return cleaned

    class Meta:
        model = PlanningItem
        # Reihenfolge: Kernfelder zuerst, Spezialfelder (zuletzt erledigt von,
        # Link) zuletzt - das Formular-Template blendet diese unter
        # „Weitere Optionen“ ein.
        fields = ["category", "title", "everyone", "status", "people", "todo_period", "due_date", "published_on", "publication_url", "notes", "last_done_by", "reference_url"]
        labels = {"title": "Aufgabe / Anfrage / Reminder", "people": "Verantwortlich"}
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "due_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "published_on": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        }


class PlanningStatusForm(forms.ModelForm):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    class Meta:
        model = PlanningItem
        fields = ["status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["revision"] = self.instance.revision


class PlanningPeopleForm(forms.ModelForm):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    class Meta:
        model = PlanningItem
        fields = ["people"]
        labels = {"people": "Verantwortlich"}
        help_texts = {"people": "Ein oder mehrere Namen, mit Komma getrennt. Leer lassen, wenn noch niemand zuständig ist."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["revision"] = self.instance.revision


class AdvertReportForm(forms.Form):
    title = forms.CharField(label="Werbeort / Aktion", max_length=200, help_text="Zum Beispiel Edeka Neu-Anspach oder Feldbergcenter Neu-Anspach.")
    done = forms.BooleanField(label="Hier wurde bereits Werbung gemacht", required=False, initial=True)
    published_on = forms.DateField(label="Wann?", required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    include_self = forms.BooleanField(label="Mich selbst als verantwortlich / mitwirkend eintragen", required=False, initial=True)
    collaborators = forms.CharField(label="Verantwortliche / Mitwirkende", max_length=200, required=False, help_text="Trage hier die beteiligten Personen ein, zum Beispiel Alex und Sam. Wenn du nur für andere einträgst, entferne oben das Häkchen. Ohne Häkchen und Namen bleibt die Zuständigkeit offen.")
    notes = forms.CharField(label="Notiz", max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    revision = forms.IntegerField(min_value=0, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        if item:
            del self.fields["title"]
            del self.fields["done"]
            self.fields["revision"].required = True
            self.initial["revision"] = item.revision


class DutyScheduleForm(forms.Form):
    """Legt zuerst ein Uhrzeitraster fest (Beginn, Ende, Stunden je Zeitraum)
    und erstellt dann jeden Dienst einmal je Zeitraum – zusätzlich optional
    einen Springer-Dienst je Zeitraum. Auf-/Abbau sind einzelne Dienste außer-
    halb des Rasters und werden über DutyForm angelegt."""

    day = DaySingleChoiceField(
        queryset=EventDay.objects.none(), label="Tag", required=False, empty_label="Kein bestimmter Tag"
    )
    start_time = forms.TimeField(label="Beginn", widget=forms.TimeInput(attrs={"type": "time"}))
    end_time = forms.TimeField(label="Ende", widget=forms.TimeInput(attrs={"type": "time"}))
    slot_hours = forms.DecimalField(
        label="Stunden je Zeitraum", min_value=0.25, max_value=24, decimal_places=2, initial=2,
        help_text="Der letzte Zeitraum endet spätestens zur angegebenen Endzeit.",
    )
    titles = forms.CharField(
        label="Dienste",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Ein Dienst je Zeile; jeder Dienst wird für jeden Zeitraum des Rasters angelegt.",
    )
    needed_people = forms.IntegerField(
        label="Benötigte Personen je Dienst", min_value=1, required=False,
        help_text="Leer lassen, wenn keine feste Zielzahl nötig ist.",
    )
    create_reserve = forms.BooleanField(
        label="Springer-Dienst je Zeitraum anlegen", required=False, initial=True,
    )

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is not None:
            self.fields["day"].queryset = event.days.all()

    def clean_titles(self):
        titles = [title for title in (line.strip() for line in self.cleaned_data["titles"].splitlines()) if title]
        if not titles:
            raise ValidationError("Mindestens ein Dienst ist erforderlich.")
        return titles

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        if start_time and end_time and start_time >= end_time:
            raise ValidationError("Das Ende muss nach dem Beginn liegen.")
        return cleaned_data


class AdvertIdentityForm(forms.ModelForm):
    class Meta:
        from .models import AdvertIdentity
        model = AdvertIdentity
        fields = ["name", "aliases"]
        widgets = {"aliases": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"aliases": "Zum Beispiel m.muster; Erika Muster. Ein Alias pro Zeile oder mit Semikolon getrennt. Gilt für diese Veranstaltung."}

    def clean(self):
        from .advert_score import split_names, name_key
        cleaned = super().clean()
        name = cleaned.get("name", "").strip()
        if len(split_names(name)) != 1:
            self.add_error("name", "Bitte genau eine Person angeben.")
        names = {name_key(n) for n in [name, *split_names(cleaned.get("aliases", ""))] if n}
        for identity in self.instance.event.advert_identities.exclude(pk=self.instance.pk):
            other = {name_key(n) for n in [identity.name, *split_names(identity.aliases)]}
            if names & other:
                raise forms.ValidationError("Ein Name ist bereits einer anderen Person zugeordnet. Bitte bearbeite die bestehende Zuordnung.")
        return cleaned
