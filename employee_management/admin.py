"""
Django Admin Configuration for Employee Management

Provides comprehensive admin interface for managing employees,
activity logs, login history, and approvals.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Q, Count
from django.utils import timezone

from auth_app.models import EmployeeUser
from .models import (
    Department,
    ManagedEmployee,
    EmployeeActivityLog,
    EmployeeLoginHistory,
    EmployeeProfilePicture,
    EmployeeApprovalQueue
)
from .services import EmployeeService


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    """
    Admin interface for managing departments.
    Allows creation, editing, and organization of departments.
    """
    
    list_display = [
        'name',
        'slug',
        'head',
        'employee_count_display',
        'is_active_badge',
        'created_at'
    ]
    
    list_filter = [
        'is_active',
        'created_at',
        'updated_at'
    ]
    
    search_fields = [
        'name',
        'slug',
        'description'
    ]
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'slug', 'description')
        }),
        ('Management', {
            'fields': ('head', 'is_active', 'color')
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ('slug', 'created_at', 'updated_at')
    prepopulated_fields = {'slug': ('name',)}
    
    def employee_count_display(self, obj):
        """Display employee count with formatted styling"""
        count = obj.employee_count
        return format_html(
            '<span style="background-color: #e8f5e9; padding: 4px 8px; border-radius: 4px; color: #2e7d32;"><strong>{}</strong> employees</span>',
            count
        )
    employee_count_display.short_description = 'Employees'
    
    def is_active_badge(self, obj):
        """Display active status with badge styling"""
        if obj.is_active:
            return format_html(
                '<span style="background-color: #c8e6c9; color: #2e7d32; padding: 4px 8px; border-radius: 4px;"><strong>Active</strong></span>'
            )
        else:
            return format_html(
                '<span style="background-color: #ffcccc; color: #d32f2f; padding: 4px 8px; border-radius: 4px;"><strong>Inactive</strong></span>'
            )
    is_active_badge.short_description = 'Status'


@admin.register(ManagedEmployee)
class ManagedEmployeeAdmin(admin.ModelAdmin):
    """
    Admin interface for managing employees.
    """
    
    list_display = [
        'full_name',
        'email',
        'department',
        'role_badge',
        'status_badge',
        'active_badge',
        'created_at'
    ]
    
    list_filter = [
        'role',
        'status',
        'is_active',
        'department',
        'created_at',
        'updated_at'
    ]
    
    search_fields = [
        'full_name',
        'email',
        'department'
    ]
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('full_name', 'email', 'department')
        }),
        ('Authentication', {
            'fields': ('password',),
            'classes': ('collapse',)
        }),
        ('Role & Status', {
            'fields': ('role', 'status', 'is_active', 'is_staff')
        }),
        ('Permissions', {
            'fields': ('groups', 'user_permissions'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ['created_at', 'updated_at', 'password']
    
    actions = [
        'activate_employees',
        'deactivate_employees',
        'approve_employees',
        'reject_employees'
    ]
    
    def role_badge(self, obj):
        """Display role with color coding."""
        role_colors = {
            'SUPER_ADMIN': '#ff4757',
            'ADMIN': '#ff7675',
            'DEPARTMENT_ADMIN': '#a29bfe',
            'EMPLOYEE': '#74b9ff'
        }
        color = role_colors.get(obj.role, '#636e72')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-weight: bold;">{}</span>',
            color,
            obj.get_role_display()
        )
    role_badge.short_description = 'Role'
    
    def status_badge(self, obj):
        """Display status with color coding."""
        status_colors = {
            'APPROVED': '#27ae60',
            'PENDING': '#f39c12',
            'REJECTED': '#e74c3c'
        }
        color = status_colors.get(obj.status, '#95a5a6')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display()
        )
    status_badge.short_description = 'Status'
    
    def active_badge(self, obj):
        """Display active status."""
        if obj.is_active:
            return format_html(
                '<span style="background-color: #27ae60; color: white; padding: 3px 8px; border-radius: 3px;">Active</span>'
            )
        else:
            return format_html(
                '<span style="background-color: #95a5a6; color: white; padding: 3px 8px; border-radius: 3px;">Inactive</span>'
            )
    active_badge.short_description = 'Active'
    
    def activate_employees(self, request, queryset):
        """Bulk activate employees."""
        count = 0
        for employee in queryset:
            if not employee.is_active:
                EmployeeService.activate_employee(employee, request.user)
                count += 1
        self.message_user(request, f"{count} employees activated.")
    activate_employees.short_description = "Activate selected employees"
    
    def deactivate_employees(self, request, queryset):
        """Bulk deactivate employees."""
        count = 0
        for employee in queryset:
            if employee.is_active:
                EmployeeService.deactivate_employee(employee, request.user)
                count += 1
        self.message_user(request, f"{count} employees deactivated.")
    deactivate_employees.short_description = "Deactivate selected employees"
    
    def approve_employees(self, request, queryset):
        """Bulk approve employees."""
        count = queryset.filter(status='PENDING').update(status='APPROVED')
        self.message_user(request, f"{count} employees approved.")
    approve_employees.short_description = "Approve selected employees"
    
    def reject_employees(self, request, queryset):
        """Bulk reject employees."""
        count = queryset.filter(status='PENDING').update(status='REJECTED', is_active=False)
        self.message_user(request, f"{count} employees rejected.")
    reject_employees.short_description = "Reject selected employees"


@admin.register(EmployeeActivityLog)
class EmployeeActivityLogAdmin(admin.ModelAdmin):
    """
    Admin interface for viewing employee activity logs.
    Read-only to maintain audit trail integrity.
    """
    
    list_display = [
        'employee',
        'activity_type_badge',
        'performed_by',
        'description',
        'created_at'
    ]
    
    list_filter = [
        'activity_type',
        'created_at',
        'employee__department',
    ]
    
    search_fields = [
        'employee__full_name',
        'employee__email',
        'performed_by__full_name',
        'description'
    ]
    
    fieldsets = (
        ('Activity Information', {
            'fields': ('employee', 'activity_type', 'performed_by', 'description')
        }),
        ('Changes', {
            'fields': ('changes',),
            'classes': ('collapse',)
        }),
        ('Request Information', {
            'fields': ('ip_address', 'user_agent'),
            'classes': ('collapse',)
        }),
        ('Timestamp', {
            'fields': ('created_at',)
        }),
    )
    
    readonly_fields = [
        'employee',
        'activity_type',
        'performed_by',
        'description',
        'changes',
        'ip_address',
        'user_agent',
        'created_at'
    ]
    
    def has_add_permission(self, request):
        """Disable manual addition of activity logs."""
        return False
    
    def has_delete_permission(self, request, obj=None):
        """Disable deletion of activity logs."""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Disable editing of activity logs."""
        return False
    
    def activity_type_badge(self, obj):
        """Display activity type with color coding."""
        activity_colors = {
            'CREATE': '#3498db',
            'UPDATE': '#2ecc71',
            'DELETE': '#e74c3c',
            'ACTIVATE': '#27ae60',
            'DEACTIVATE': '#95a5a6',
            'APPROVE': '#27ae60',
            'REJECT': '#e74c3c',
            'ROLE_CHANGE': '#f39c12',
            'DEPARTMENT_CHANGE': '#9b59b6',
            'PASSWORD_RESET': '#e67e22',
            'STATUS_CHANGE': '#3498db',
            'LOGIN': '#2ecc71',
            'LOGOUT': '#95a5a6',
            'EXPORT': '#16a085',
            'BULK_ACTION': '#34495e'
        }
        color = activity_colors.get(obj.activity_type, '#95a5a6')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-weight: bold;">{}</span>',
            color,
            obj.get_activity_type_display()
        )
    activity_type_badge.short_description = 'Activity Type'


@admin.register(EmployeeLoginHistory)
class EmployeeLoginHistoryAdmin(admin.ModelAdmin):
    """
    Admin interface for viewing login history.
    Read-only for audit purposes.
    """
    
    list_display = [
        'employee',
        'login_at',
        'logout_at',
        'session_duration_display',
        'ip_address',
        'is_active_badge'
    ]
    
    list_filter = [
        'login_at',
        'is_active',
        'employee__department'
    ]
    
    search_fields = [
        'employee__full_name',
        'employee__email',
        'ip_address',
        'session_key'
    ]
    
    fieldsets = (
        ('Session Information', {
            'fields': ('employee', 'login_at', 'logout_at', 'is_active')
        }),
        ('Device Information', {
            'fields': ('ip_address', 'user_agent', 'session_key'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = [
        'employee',
        'login_at',
        'logout_at',
        'ip_address',
        'user_agent',
        'session_key',
        'is_active'
    ]
    
    def has_add_permission(self, request):
        """Disable manual addition of login records."""
        return False
    
    def has_delete_permission(self, request, obj=None):
        """Disable deletion of login records."""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Disable editing of login records."""
        return False
    
    def session_duration_display(self, obj):
        """Display session duration."""
        if obj.session_duration:
            return f"{obj.session_duration} min"
        return "—"
    session_duration_display.short_description = 'Duration'
    
    def is_active_badge(self, obj):
        """Display active session status."""
        if obj.is_active:
            return format_html(
                '<span style="background-color: #27ae60; color: white; padding: 3px 8px; border-radius: 3px;">Active</span>'
            )
        else:
            return format_html(
                '<span style="background-color: #95a5a6; color: white; padding: 3px 8px; border-radius: 3px;">Closed</span>'
            )
    is_active_badge.short_description = 'Status'


@admin.register(EmployeeProfilePicture)
class EmployeeProfilePictureAdmin(admin.ModelAdmin):
    """
    Admin interface for managing employee profile pictures.
    """
    
    list_display = [
        'employee',
        'uploaded_at',
        'is_current_badge',
        'image_preview'
    ]
    
    list_filter = [
        'is_current',
        'uploaded_at',
        'employee__department'
    ]
    
    search_fields = [
        'employee__full_name',
        'employee__email'
    ]
    
    readonly_fields = [
        'employee',
        'uploaded_at',
        'image_preview'
    ]
    
    def is_current_badge(self, obj):
        """Display if picture is current."""
        if obj.is_current:
            return format_html(
                '<span style="background-color: #27ae60; color: white; padding: 3px 8px; border-radius: 3px;">Current</span>'
            )
        else:
            return format_html(
                '<span style="background-color: #95a5a6; color: white; padding: 3px 8px; border-radius: 3px;">Archive</span>'
            )
    is_current_badge.short_description = 'Status'
    
    def image_preview(self, obj):
        """Display image preview."""
        if obj.image:
            return format_html(
                '<img src="{}" width="100" height="100" style="border-radius: 5px;" />',
                obj.image.url
            )
        return "—"
    image_preview.short_description = 'Preview'


@admin.register(EmployeeApprovalQueue)
class EmployeeApprovalQueueAdmin(admin.ModelAdmin):
    """
    Admin interface for managing employee approvals.
    """
    
    list_display = [
        'employee',
        'priority_badge',
        'status_badge',
        'submitted_at',
        'reviewed_by'
    ]
    
    list_filter = [
        'priority',
        'is_approved',
        'submitted_at',
        'reviewed_at'
    ]
    
    search_fields = [
        'employee__full_name',
        'employee__email',
        'submitted_by__full_name'
    ]
    
    fieldsets = (
        ('Employee Information', {
            'fields': ('employee', 'submitted_by')
        }),
        ('Submission Details', {
            'fields': ('priority', 'notes', 'submitted_at')
        }),
        ('Review Details', {
            'fields': ('reviewed_by', 'reviewed_at', 'is_approved', 'approval_notes')
        }),
    )
    
    readonly_fields = ['submitted_at', 'reviewed_at']
    
    actions = ['approve_from_queue', 'reject_from_queue']
    
    def priority_badge(self, obj):
        """Display priority with color coding."""
        priority_colors = {
            'LOW': '#3498db',
            'MEDIUM': '#f39c12',
            'HIGH': '#e74c3c',
            'URGENT': '#c0392b'
        }
        color = priority_colors.get(obj.priority, '#95a5a6')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-weight: bold;">{}</span>',
            color,
            obj.get_priority_display()
        )
    priority_badge.short_description = 'Priority'
    
    def status_badge(self, obj):
        """Display approval status."""
        if obj.is_approved:
            return format_html(
                '<span style="background-color: #27ae60; color: white; padding: 3px 8px; border-radius: 3px;">Approved</span>'
            )
        else:
            return format_html(
                '<span style="background-color: #f39c12; color: white; padding: 3px 8px; border-radius: 3px;">Pending</span>'
            )
    status_badge.short_description = 'Status'
    
    def approve_from_queue(self, request, queryset):
        """Approve selected employees from queue."""
        count = 0
        for item in queryset.filter(is_approved=False):
            EmployeeService.approve_employee(
                item.employee,
                request.user,
                "Approved from admin queue"
            )
            count += 1
        self.message_user(request, f"{count} employees approved.")
    approve_from_queue.short_description = "Approve selected employees"
    
    def reject_from_queue(self, request, queryset):
        """Reject selected employees from queue."""
        count = 0
        for item in queryset.filter(is_approved=False):
            EmployeeService.reject_employee(
                item.employee,
                request.user,
                "Rejected from admin queue"
            )
            count += 1
        self.message_user(request, f"{count} employees rejected.")
    reject_from_queue.short_description = "Reject selected employees"
