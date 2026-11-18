from django.contrib import admin

from .models import AccessChange, QuoteFile, Remark, TrackerAccess, TrackerLog, VendorEnquiry


class QuoteFileInline(admin.TabularInline):
    model = QuoteFile
    extra = 0


class RemarkInline(admin.TabularInline):
    model = Remark
    extra = 0


@admin.register(VendorEnquiry)
class VendorEnquiryAdmin(admin.ModelAdmin):
    list_display = ("enquiry_no", "vendor_name", "customer_name", "status", "created_by", "updated_at")
    list_filter = ("status",)
    search_fields = ("enquiry_no", "vendor_name", "customer_name", "vendor_quote_no")
    inlines = [QuoteFileInline, RemarkInline]


@admin.register(TrackerAccess)
class TrackerAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "level", "granted_by", "updated_at")


admin.site.register(AccessChange)
admin.site.register(TrackerLog)
