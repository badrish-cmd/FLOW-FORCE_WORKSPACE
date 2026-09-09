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
        "project_name",
        "customer_name",
        "pid_reference",
        "drawing_type",
        "parent",
        "active_revision_number",
        "drafter_name",
        "updated_at",
    )
    search_fields = (
        "base_drawing_number",
        "drawing_name",
        "project_name",
        "customer_name",
        "pid_reference",
        "enquiry_number",
        "po_number",
    )
    list_filter = ("drawing_type", "format", "watermark_company", "updated_at")
    inlines = [DrawingRevisionInline, DrawingAccessInline]


@admin.register(DrawingRevision)
class DrawingRevisionAdmin(admin.ModelAdmin):
    list_display = (
        "drawing",
        "revision_number",
        "original_pdf_filename",
        "original_native_filename",
        "drafter_name",
        "revision_date",
        "created_at",
    )
    search_fields = ("drawing__base_drawing_number", "revision_number", "original_pdf_filename", "drafter_name")
    list_filter = ("revision_date",)


@admin.register(DrawingAccess)
class DrawingAccessAdmin(admin.ModelAdmin):
    list_display = ("drawing", "user", "department", "access_level", "granted_by", "created_at")
    list_filter = ("access_level", "created_at")
