from django.contrib import admin

from .models import TaskCell
from .models import TaskAssignment
from .models import TaskAttachment
from .models import TaskComment
from .models import TaskFilter
from .models import TaskHistory
from .models import Notification
from .models import TaskRow
from .models import Tracker
from .models import TrackerColumn
from .services import ensure_mandatory_columns


class TrackerColumnInline(admin.TabularInline):
    model = TrackerColumn
    extra = 0
    fields = ("label", "key", "position", "is_fixed", "is_active")
    readonly_fields = ("key", "is_fixed")


@admin.register(Tracker)
class TrackerAdmin(admin.ModelAdmin):
    list_display = ("department", "name", "is_active", "created_at")
    list_filter = ("department", "is_active")
    search_fields = ("department", "name")
    inlines = [TrackerColumnInline]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        ensure_mandatory_columns(obj)


@admin.register(TrackerColumn)
class TrackerColumnAdmin(admin.ModelAdmin):
    list_display = ("tracker", "label", "key", "position", "is_fixed", "is_active")
    list_filter = ("tracker", "is_fixed", "is_active")
    search_fields = ("label", "key", "tracker__department")


class TaskCellInline(admin.TabularInline):
    model = TaskCell
    extra = 0


@admin.register(TaskRow)
class TaskRowAdmin(admin.ModelAdmin):
    list_display = ("tracker", "s_no", "task_name", "priority", "assigned_to", "assigned_by", "due_date", "status", "initial_mail", "alert_mail")
    list_filter = ("tracker__department", "status", "initial_mail", "alert_mail")
    search_fields = ("task_name", "assigned_to__full_name", "assigned_to__email")
    inlines = [TaskCellInline]


@admin.register(TaskCell)
class TaskCellAdmin(admin.ModelAdmin):
    list_display = ("row", "column", "value")
    search_fields = ("value", "column__label", "row__task_name")


@admin.register(TaskAssignment)
class TaskAssignmentAdmin(admin.ModelAdmin):
    list_display = ("row", "user", "assignment_type", "is_primary", "assigned_by", "created_at")
    list_filter = ("assignment_type", "is_primary")
    search_fields = ("row__task_name", "user__full_name", "user__email")


@admin.register(TaskComment)
class TaskCommentAdmin(admin.ModelAdmin):
    list_display = ("row", "created_by", "internal", "created_at")
    search_fields = ("content", "row__task_name", "created_by__full_name")


@admin.register(TaskAttachment)
class TaskAttachmentAdmin(admin.ModelAdmin):
    list_display = ("row", "original_name", "uploaded_by", "created_at")
    search_fields = ("original_name", "row__task_name", "uploaded_by__full_name")


@admin.register(TaskHistory)
class TaskHistoryAdmin(admin.ModelAdmin):
    list_display = ("row", "action", "field_name", "changed_by", "created_at")
    search_fields = ("row__task_name", "action", "field_name")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "notif_type", "read", "sent_at")
    list_filter = ("notif_type", "read")
    search_fields = ("user__full_name", "user__email")


@admin.register(TaskFilter)
class TaskFilterAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "tracker", "is_default", "created_at")
    search_fields = ("name", "user__full_name", "tracker__name")
