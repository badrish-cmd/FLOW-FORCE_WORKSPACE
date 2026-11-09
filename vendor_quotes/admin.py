from django.contrib import admin

from .models import (AIRequestLog, Enquiry, MasterSheet, QuoteChange, QuoteEvent, QuoteRevision, Vendor,
                     VendorMasterFile, VendorQuote)


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "currency", "default_exchange_rate", "default_margin_factor", "is_active")
    search_fields = ("name", "code")


@admin.register(VendorMasterFile)
class VendorMasterFileAdmin(admin.ModelAdmin):
    list_display = ("vendor", "note", "sheet_count", "created_at")
    list_filter = ("vendor",)


@admin.register(MasterSheet)
class MasterSheetAdmin(admin.ModelAdmin):
    list_display = ("vendor", "sheet_name", "enquiry_no", "customer_name", "quotation_no", "details_missing")
    list_filter = ("vendor", "details_missing")
    search_fields = ("enquiry_no", "customer_name", "sheet_name", "quotation_no")
    exclude = ("snapshot",)


@admin.register(Enquiry)
class EnquiryAdmin(admin.ModelAdmin):
    list_display = ("number", "customer_name", "zoho_customer_id", "created_at")
    search_fields = ("number", "customer_name")


class RevisionInline(admin.TabularInline):
    model = QuoteRevision
    fields = ("number", "status", "excel_status", "zoho_status", "zoho_estimate_number")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(VendorQuote)
class VendorQuoteAdmin(admin.ModelAdmin):
    list_display = ("enquiry", "vendor", "vendor_quote_no", "created_by", "created_at")
    list_filter = ("vendor",)
    search_fields = ("enquiry__number", "vendor_quote_no", "enquiry__customer_name")
    inlines = [RevisionInline]


admin.site.register(QuoteChange)
admin.site.register(QuoteEvent)


@admin.register(AIRequestLog)
class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "quote", "feature", "model", "file_name", "status", "duration_ms", "total_tokens", "estimated_cost_usd")
    list_filter = ("status", "feature", "model", "provider", "created_at")
    search_fields = ("file_name", "user__email", "user__full_name", "quote__vendor_quote_no", "error_message")
    readonly_fields = [f.name for f in AIRequestLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
