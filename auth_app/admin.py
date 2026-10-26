from django.contrib import admin
from .models import EmployeeUser
from .models import PasswordResetOTP

admin.site.register(PasswordResetOTP)


@admin.register(EmployeeUser)
class EmployeeUserAdmin(admin.ModelAdmin):

    list_display = (
        'full_name',
        'email',
        'department',
        'role',
        'status',
        'is_active',
        'is_staff',
    )

    list_filter = (
        'role',
        'status',
        'department',
        'is_active',
        'is_staff',
    )

    search_fields = (
        'full_name',
        'email',
    )

    ordering = (
        'full_name',
    )


from django.utils import timezone
from django.utils.html import format_html
from .models import AuthorizedDevice


@admin.register(AuthorizedDevice)
class AuthorizedDeviceAdmin(admin.ModelAdmin):
    list_display = (
        'device_name',
        'requested_by_name',
        'assigned_user',
        'department',
        'status_badge',
        'registration_ip',
        'last_seen_ip',
        'last_used_at',
        'created_at',
        'approved_by',
    )
    list_filter = (
        'status',
        'department',
        'created_at',
        'last_used_at',
    )
    search_fields = (
        'device_name',
        'asset_tag',
        'requested_by_name',
        'employee_email',
        'assigned_user__email',
        'assigned_user__full_name',
        'registration_ip',
        'last_seen_ip',
    )
    readonly_fields = (
        'id',
        'device_token',
        'created_at',
        'last_used_at',
        'registration_ip',
        'last_seen_ip',
        'user_agent_summary',
        'approved_at',
        'approved_by',
    )
    fieldsets = (
        ('Device Information', {
            'fields': ('device_name', 'asset_tag', 'status', 'device_token')
        }),
        ('User & Ownership', {
            'fields': ('requested_by_name', 'employee_email', 'department', 'assigned_user')
        }),
        ('Approval Tracking', {
            'fields': ('approved_by', 'approved_at')
        }),
        ('Diagnostics & Audit', {
            'fields': ('registration_ip', 'last_seen_ip', 'user_agent_summary', 'created_at', 'last_used_at')
        }),
    )
    actions = [
        'approve_selected_devices',
        'revoke_selected_devices',
        'reject_selected_devices',
        'reset_to_pending',
    ]

    def status_badge(self, obj):
        colors = {
            'APPROVED': '#10B981',
            'PENDING': '#F59E0B',
            'REJECTED': '#EF4444',
            'REVOKED': '#64748B',
        }
        color = colors.get(obj.status, '#64748B')
        return format_html(
            '<span style="background-color: {}; color: #ffffff; padding: 3px 10px; border-radius: 12px; font-weight: 600; font-size: 11px;">{}</span>',
            color,
            obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    @admin.action(description='Approve selected devices')
    def approve_selected_devices(self, request, queryset):
        count = queryset.update(
            status='APPROVED',
            approved_at=timezone.now(),
            approved_by=request.user
        )
        self.message_user(request, f'Successfully approved {count} device(s).')

    @admin.action(description='Revoke / Block selected devices')
    def revoke_selected_devices(self, request, queryset):
        count = queryset.update(
            status='REVOKED'
        )
        self.message_user(request, f'Successfully revoked {count} device(s). Access is now blocked.')

    @admin.action(description='Reject selected devices')
    def reject_selected_devices(self, request, queryset):
        count = queryset.update(
            status='REJECTED'
        )
        self.message_user(request, f'Successfully rejected {count} device(s).')

    @admin.action(description='Reset to Pending Approval')
    def reset_to_pending(self, request, queryset):
        count = queryset.update(
            status='PENDING',
            approved_at=None,
            approved_by=None
        )
        self.message_user(request, f'Reset {count} device(s) back to Pending status.')