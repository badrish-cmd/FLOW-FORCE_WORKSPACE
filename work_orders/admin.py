from django.contrib import admin

from .models import Attachment, ChecklistItem, Event, NumberCounter, Team, WorkOrder, WorkOrderAccess, WorkOrderLine


class LineInline(admin.TabularInline):
    model = WorkOrderLine
    extra = 0


class ChecklistInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "pid", "team", "customer_name", "status", "required_by", "updated_at")
    list_filter = ("status", "team")
    search_fields = ("number", "pid", "customer_name", "customer_po", "job_no")
    inlines = [LineInline, ChecklistInline]


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "position")


admin.site.register(WorkOrderAccess)
admin.site.register(NumberCounter)
admin.site.register(Attachment)
admin.site.register(Event)
