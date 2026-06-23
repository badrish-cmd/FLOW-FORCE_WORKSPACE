from django.urls import path

from .views import (
    employee_list,
    employee_create,
    employee_detail,
    employee_edit,
    employee_activate,
    employee_deactivate,
    employee_approve,
    employee_reject,
    employee_reset_password,
    employee_delete,
    bulk_action,
    approval_center,
    export_employees,
    activity_history,
    login_history,
    employee_status_action,  # Deprecated but kept for backward compatibility
    global_activity_logs,
)


urlpatterns = [
    # Employee List & CRUD
    path(
        "",
        employee_list,
        name="employee_list",
    ),
    path(
        "create/",
        employee_create,
        name="employee_create",
    ),
    path(
        "<int:employee_id>/",
        employee_detail,
        name="employee_detail",
    ),
    path(
        "<int:employee_id>/edit/",
        employee_edit,
        name="employee_edit",
    ),
    
    # Employee Actions
    path(
        "<int:employee_id>/activate/",
        employee_activate,
        name="employee_activate",
    ),
    path(
        "<int:employee_id>/deactivate/",
        employee_deactivate,
        name="employee_deactivate",
    ),
    path(
        "<int:employee_id>/delete/",
        employee_delete,
        name="employee_delete",
    ),
    path(
        "<int:employee_id>/approve/",
        employee_approve,
        name="employee_approve",
    ),
    path(
        "<int:employee_id>/reject/",
        employee_reject,
        name="employee_reject",
    ),
    path(
        "<int:employee_id>/reset-password/",
        employee_reset_password,
        name="employee_reset_password",
    ),
    
    # Bulk Actions
    path(
        "bulk/action/",
        bulk_action,
        name="bulk_action",
    ),
    
    # Approvals
    path(
        "approvals/",
        approval_center,
        name="approval_center",
    ),
    
    # Export
    path(
        "export/",
        export_employees,
        name="export_employees",
    ),
    
    # History Views
    path(
        "activity-logs/",
        global_activity_logs,
        name="global_activity_logs",
    ),
    path(
        "<int:employee_id>/activity-history/",
        activity_history,
        name="activity_history",
    ),
    path(
        "<int:employee_id>/login-history/",
        login_history,
        name="login_history",
    ),
    
    # Deprecated - Backward Compatibility
    path(
        "<int:employee_id>/<str:action>/",
        employee_status_action,
        name="employee_status_action",
    ),
]
