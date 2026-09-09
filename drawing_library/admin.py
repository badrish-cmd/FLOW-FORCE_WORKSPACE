from django.contrib import admin
from .models import EngineeringDrawing, DrawingRevision, DrawingAccess


class DrawingRevisionInline(admin.TabularInline):
    model = DrawingRevision
    extra = 0
    readonly_fields = ("created_at",)


class DrawingAccessInline(admin.TabularInline):
    model = DrawingAccess
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(EngineeringDrawing)
class EngineeringDrawingAdmin(admin.ModelAdmin):
    list_display = (
        "base_drawing_number",
        "drawing_name",
        "customer_name",
        "pid_reference",
        "active_revision_number",
        "format",
        "watermark_company",
        "drafter_name",
        "updated_at",
    )
    search_fields = (
        "base_drawing_number",
        "drawing_name",
        "customer_name",
        "pid_reference",
        "enquiry_number",
        "po_number",
    )
    list_filter = ("format", "watermark_company", "updated_at")
    inlines = [DrawingRevisionInline, DrawingAccessInline]


@admin.register(DrawingRevision)
class DrawingRevisionAdmin(admin.ModelAdmin):
    list_display = (
        "drawing",
        "revision_number",
        "stage_change_description",
        "drafter_name",
        "revision_date",
        "created_at",
    )
    search_fields = ("drawing__base_drawing_number", "revision_number", "drafter_name")
    list_filter = ("revision_date",)


@admin.register(DrawingAccess)
class DrawingAccessAdmin(admin.ModelAdmin):
    list_display = ("drawing", "user", "department", "access_level", "granted_by", "created_at")
    list_filter = ("access_level", "created_at")
