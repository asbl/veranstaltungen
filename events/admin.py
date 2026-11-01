from django.contrib import admin
from django import forms

from .models import (
    Answer,
    AnswerLike,
    AnswerReaction,
    Attendance,
    BroughtPerson,
    Decline,
    Duty,
    DutySignup,
    Event,
    EventDay,
    GameBringClaim,
    GameComment,
    GameEntry,
    GameInterest,
    Idea,
    IdeaComment,
    IdeaCommentNotification,
    IdeaLike,
    OrgaActionLog,
    PlanningItem,
    PotluckClaim,
    PotluckItem,
    Question,
    QuestionLike,
    QuestionReaction,
    Slot,
    SlotComment,
    SlotSignup,
    SiteSettings,
    UserProfile,
)


class EventDayInline(admin.TabularInline):
    model = EventDay
    extra = 1


class BroughtPersonInline(admin.TabularInline):
    model = BroughtPerson
    extra = 0


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "location", "has_access_password", "created_by", "created_at")
    filter_horizontal = ("editors",)
    inlines = [EventDayInline]


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ("event", "user", "name", "role")
    list_filter = ("event", "role")
    inlines = [BroughtPersonInline]


@admin.register(PotluckItem)
class PotluckItemAdmin(admin.ModelAdmin):
    list_display = ("title", "event", "needed_quantity", "unit", "variable_quantity", "variable_per_persons", "target_quantity", "remaining", "created_by")
    list_filter = ("event",)


@admin.register(PotluckClaim)
class PotluckClaimAdmin(admin.ModelAdmin):
    list_display = ("item", "user", "name", "quantity", "entered_by")


@admin.register(Idea)
class IdeaAdmin(admin.ModelAdmin):
    list_display = ("text", "event", "author", "created_at")
    list_filter = ("event",)


admin.site.register(IdeaComment)
admin.site.register(IdeaLike)


@admin.register(IdeaCommentNotification)
class IdeaCommentNotificationAdmin(admin.ModelAdmin):
    list_display = ("recipient", "comment", "created_at", "sent_at")
    list_filter = ("sent_at",)


@admin.register(Slot)
class SlotAdmin(admin.ModelAdmin):
    list_display = ("title", "event", "day", "start_time", "end_time", "max_participants", "signup_count")
    list_filter = ("event", "day")


admin.site.register(SlotSignup)
admin.site.register(SlotComment)


@admin.register(GameEntry)
class GameEntryAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "type", "bgg_id", "min_players", "max_players", "bgg_rating", "bgg_synced_at", "created_by")
    list_filter = ("event", "type")


admin.site.register(GameComment)
admin.site.register(GameInterest)
admin.site.register(GameBringClaim)
admin.site.register(EventDay)
admin.site.register(BroughtPerson)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name")
    search_fields = ("user__username", "display_name")


@admin.register(Duty)
class DutyAdmin(admin.ModelAdmin):
    list_display = ("title", "event", "day", "needed_people", "signup_count", "created_by")
    list_filter = ("event", "day")


admin.site.register(DutySignup)


@admin.register(Decline)
class DeclineAdmin(admin.ModelAdmin):
    list_display = ("event", "user", "created_at")
    list_filter = ("event",)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("text", "event", "author", "created_at")
    list_filter = ("event",)


admin.site.register(Answer)
admin.site.register(QuestionLike)
admin.site.register(QuestionReaction)
admin.site.register(AnswerLike)
admin.site.register(AnswerReaction)


@admin.register(OrgaActionLog)
class OrgaActionLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "event", "description")
    list_filter = ("event", "actor")
    readonly_fields = ("event", "actor", "description", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class PlanningItemAdminForm(forms.ModelForm):
    edit_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput, required=False)

    class Meta:
        model = PlanningItem
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["edit_revision"].required = bool(self.instance.pk)
        self.initial["edit_revision"] = self.instance.revision

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            # Django's admin wraps change-form POSTs in a transaction.
            current = PlanningItem.objects.select_for_update().get(pk=self.instance.pk)
            if cleaned.get("edit_revision") != current.revision:
                raise forms.ValidationError("Dieser Eintrag wurde inzwischen geändert. Bitte lade den aktuellen Stand und gleiche deine Eingaben ab.")
        return cleaned


@admin.register(PlanningItem)
class PlanningItemAdmin(admin.ModelAdmin):
    form = PlanningItemAdminForm
    list_display = ("title", "event", "category", "status", "people", "last_done_by", "due_date", "published_on")
    list_filter = ("event", "category", "status")

    def save_model(self, request, obj, form, change):
        from .views import _log_planning_changes
        from .forms import PlanningItemForm
        previous = None
        if change:
            old = PlanningItem.objects.get(pk=obj.pk)
            previous = {field: getattr(old, field) for field in PlanningItemForm._meta.fields}
            obj.revision = old.revision + 1
        super().save_model(request, obj, form, change)
        _log_planning_changes(request, obj.event, obj, previous)

    def delete_model(self, request, obj):
        OrgaActionLog.objects.create(event=obj.event, actor=request.user, description=f'{obj.get_category_display()}: „{obj.title}“ im Admin gelöscht')
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton: es gibt genau einen Datensatz (pk=1) mit Name und Logo des
    Betreibers. Das Logo liegt als Datei unter MEDIA_ROOT, nicht im Code."""

    fieldsets = (
        (None, {"fields": ("name", "logo")}),
    )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
