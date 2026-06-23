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