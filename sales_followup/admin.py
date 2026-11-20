from django.contrib import admin

from .models import FollowupAccess, FollowupAccessChange, FollowupEntry, FollowupLog, SalesFollowup


class EntryInline(admin.TabularInline):
    model = FollowupEntry
    extra = 0


@admin.register(SalesFollowup)
class SalesFollowupAdmin(admin.ModelAdmin):
    list_display = ("enquiry_no", "zoho_number", "customer_name", "deal_owner", "status", "next_followup_date")
    list_filter = ("status",)
    search_fields = ("enquiry_no", "zoho_number", "customer_name", "deal_owner")
    inlines = [EntryInline]


admin.site.register(FollowupAccess)
admin.site.register(FollowupAccessChange)
admin.site.register(FollowupLog)
