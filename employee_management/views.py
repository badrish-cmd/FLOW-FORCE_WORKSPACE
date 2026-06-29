"""
Employee Management Views

Handles all employee CRUD operations, filtering, bulk actions,
approvals, exports, and activity tracking.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Count
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST, require_http_methods
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.utils import timezone
from datetime import timedelta
import csv

from auth_app.models import EmployeeUser
from .models import (
    EmployeeActivityLog,
    EmployeeLoginHistory,
    EmployeeApprovalQueue
)
from .forms import EmployeeForm
from .permissions import (
    can_manage_employees,
    can_manage_employee,
    can_edit_employee_role,
    employee_access_required
)
from .services import EmployeeService, EmployeeExportService


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_visible_employees(user):
    """Get employees visible to the current user based on their role."""
    employees = EmployeeUser.objects.all().select_related("department")

    if user.role == "DEPARTMENT_ADMIN":
        return employees.filter(department=user.department)

    if user.role == "EMPLOYEE":
        return employees.filter(is_active=True)

    return employees


def get_employee_or_404(user, employee_id):
    """Get employee or raise 404 if not visible to user."""
    return get_object_or_404(
        get_visible_employees(user),
        pk=employee_id,
    )


def get_employee_stats(user):
    """Get employee statistics based on user permissions."""
    employees = get_visible_employees(user)

    return {
        "total_employees": employees.count(),
        "pending_approvals": employees.filter(status="PENDING").count(),
        "active_employees": employees.filter(is_active=True).count(),
        "inactive_employees": employees.filter(is_active=False).count(),
        "department_count": (
            employees
            .exclude(department__isnull=True)
            .values("department")
            .distinct()
            .count()
        ),
    }


# ============================================================================
# EMPLOYEE LIST & SEARCH
# ============================================================================

@login_required
@employee_access_required
def employee_list(request):
    """Display filtered list of employees with search and advanced filters."""
    
    employees = get_visible_employees(request.user).order_by(
        "-created_at"
    )

    # Filters
    search = request.GET.get("search", "").strip()
    role = request.GET.get("role", "").strip()
    status = request.GET.get("status", "").strip()
    department = request.GET.get("department", "").strip()
    active = request.GET.get("active", "").strip()
    sort_by = request.GET.get("sort_by", "-created_at").strip()

    if search:
        employees = employees.filter(
            Q(full_name__icontains=search)
            | Q(email__icontains=search)
            | Q(department__name__icontains=search)
        )

    if role:
        employees = employees.filter(role=role)

    if status:
        employees = employees.filter(status=status)

    if department:
        employees = employees.filter(department_id=department)

    if active == "yes":
        employees = employees.filter(is_active=True)
    elif active == "no":
        employees = employees.filter(is_active=False)

    # Sorting
    valid_sorts = [
        "-created_at", "created_at",
        "full_name", "-full_name",
        "email", "-email",
        "department", "-department",
        "department__name", "-department__name",
        "role", "-role",
        "status", "-status",
        "is_active", "-is_active"
    ]
    if sort_by not in valid_sorts:
        sort_by = "-created_at"
    
    employees = employees.order_by(sort_by)

    # Pagination
    page = request.GET.get("page", 1)
    paginator = Paginator(employees, 25)
    
    try:
        employees_page = paginator.page(page)
    except PageNotAnInteger:
        employees_page = paginator.page(1)
    except EmptyPage:
        employees_page = paginator.page(paginator.num_pages)

    # Get unique departments
    departments = (
        get_visible_employees(request.user)
        .exclude(department__isnull=True)
        .values_list("department__id", "department__name")
        .distinct()
        .order_by("department__name")
    )

    context = {
        "employees": employees_page,
        "paginator": paginator,
        "stats": get_employee_stats(request.user),
        "roles": EmployeeUser.ROLE_CHOICES,
        "statuses": EmployeeUser.STATUS_CHOICES,
        "departments": departments,
        "filters": {
            "search": search,
            "role": role,
            "status": status,
            "department": department,
            "active": active,
            "sort_by": sort_by
        },
        "can_manage": can_manage_employees(request.user),
        "manageable_employee_ids": [
            employee.id
            for employee in employees_page
            if can_manage_employee(request.user, employee)
        ],
        "limited_view": request.user.role == "EMPLOYEE",
    }

    return render(
        request,
        "employee_management/employee_list.html",
        context,
    )


# ============================================================================
# EMPLOYEE CRUD
# ============================================================================

@login_required
@employee_access_required
def employee_create(request):
    """Create a new employee."""
    
    if request.user.role not in ("SUPER_ADMIN", "ADMIN"):
        messages.error(
            request,
            "You do not have permission to create employees.",
        )
        return redirect("employee_list")

    if request.method == "POST":
        form = EmployeeForm(
            request.POST,
            current_user=request.user,
        )

        if form.is_valid():
            try:
                employee = EmployeeService.create_employee(
                    full_name=form.cleaned_data["full_name"],
                    email=form.cleaned_data["email"],
                    department=form.cleaned_data["department"],
                    role=form.cleaned_data["role"],
                    status=form.cleaned_data["status"],
                    password=form.cleaned_data.get("password"),
                    created_by=request.user
                )
                messages.success(
                    request,
                    f"Employee {employee.full_name} created successfully.",
                )
                return redirect(
                    "employee_detail",
                    employee_id=employee.id,
                )
            except Exception as e:
                messages.error(request, f"Error creating employee: {str(e)}")
    else:
        form = EmployeeForm(current_user=request.user)

    return render(
        request,
        "employee_management/employee_form.html",
        {
            "form": form,
            "page_title": "Create Employee",
            "submit_label": "Create Employee",
        },
    )


@login_required
@employee_access_required
def employee_detail(request, employee_id):
    if request.user.role == "EMPLOYEE":
        messages.error(request, "You do not have permission to view employee details.")
        return redirect("employee_list")

    employee = get_employee_or_404(request.user, employee_id)
    can_manage = can_manage_employee(request.user, employee)
    
    from tasks.models import ActivityLog as TaskActivityLog
    from django.db.models import Q
    
    # 1. Employee Account Activity Logs
    emp_logs = EmployeeActivityLog.objects.filter(employee=employee)
    
    # 2. Login Logs
    login_logs = EmployeeLoginHistory.objects.filter(employee=employee)
    
    # 3. Task Action Logs
    task_logs = TaskActivityLog.objects.filter(user=employee).select_related("task", "task__row", "task__row__table")
    
    # Date filter
    selected_date_str = request.GET.get("date", "").strip()
    if selected_date_str:
        try:
            from datetime import datetime
            sel_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
            emp_logs = emp_logs.filter(created_at__date=sel_date)
            login_logs = login_logs.filter(Q(login_at__date=sel_date) | Q(logout_at__date=sel_date))
            task_logs = task_logs.filter(timestamp__date=sel_date)
        except ValueError:
            pass
            
    # Sorting direction
    sort_dir = request.GET.get("sort", "desc").strip()
    if sort_dir not in ["asc", "desc"]:
        sort_dir = "desc"
        
    order_by_activity = "-created_at" if sort_dir == "desc" else "created_at"
    order_by_login = "-login_at" if sort_dir == "desc" else "login_at"
    order_by_task = "-timestamp" if sort_dir == "desc" else "timestamp"
    
    emp_logs = emp_logs.order_by(order_by_activity)
    login_logs = login_logs.order_by(order_by_login)
    task_logs = task_logs.order_by(order_by_task)
    
    # Combine them into a single timeline sorted by timestamp
    combined_timeline = []
    
    for log in emp_logs:
        combined_timeline.append({
            "timestamp": log.created_at,
            "type": "ACCOUNT",
            "title": log.get_activity_type_display(),
            "description": log.description,
            "icon": "fas fa-user-cog",
            "badge_class": "bg-info",
        })
        
    for log in login_logs:
        combined_timeline.append({
            "timestamp": log.login_at,
            "type": "LOGIN",
            "title": "Logged In",
            "description": f"Session logged in from IP: {log.ip_address or 'Unknown'}",
            "icon": "fas fa-sign-in-alt",
            "badge_class": "bg-primary",
        })
        if log.logout_at:
            combined_timeline.append({
                "timestamp": log.logout_at,
                "type": "LOGOUT",
                "title": "Logged Out",
                "description": f"Session logged out",
                "icon": "fas fa-sign-out-alt",
                "badge_class": "bg-secondary",
            })
            
    for log in task_logs:
        icon = "fas fa-tasks"
        badge_class = "bg-warning text-dark"
        if "Created" in log.action:
            icon = "fas fa-plus-circle"
            badge_class = "bg-success text-white"
        elif "Completed" in log.action or "Approved" in log.action or "status to COMPLETED" in str(log.details) or "status to APPROVED" in str(log.details):
            icon = "fas fa-check-circle"
            badge_class = "bg-success text-white"
            
        # Get worksheet and task names
        table_name = log.task.row.table.name if (log.task and log.task.row and log.task.row.table) else "Unknown"
        task_name = log.task.task_name if log.task else "Unnamed"
        
        combined_timeline.append({
            "timestamp": log.timestamp,
            "type": "WORK",
            "title": log.action,
            "description": f"Worksheet: {table_name} - Task: {task_name}",
            "icon": icon,
            "badge_class": badge_class,
        })
        
    # Sort combined timeline by timestamp
    reverse_sort = True if sort_dir == "desc" else False
    combined_timeline.sort(key=lambda x: x["timestamp"], reverse=reverse_sort)
    
    context = {
        "employee": employee,
        "can_manage": can_manage,
        "limited_view": request.user.role == "EMPLOYEE",
        "combined_timeline": combined_timeline[:30],  # Show recent 30 items
        "sort_dir": sort_dir,
        "selected_date": selected_date_str,
    }

    return render(
        request,
        "employee_management/employee_detail.html",
        context,
    )


@login_required
@employee_access_required
def employee_edit(request, employee_id):
    """Edit employee information."""
    
    employee = get_employee_or_404(request.user, employee_id)

    if not can_manage_employee(request.user, employee):
        messages.error(
            request,
            "You do not have permission to edit this employee.",
        )
        return redirect("employee_detail", employee_id=employee.id)

    role_edit_allowed = can_edit_employee_role(request.user, employee)

    if request.method == "POST":
        form = EmployeeForm(
            request.POST,
            instance=employee,
            current_user=request.user,
            can_edit_role=role_edit_allowed,
        )

        if form.is_valid():
            try:
                employee = EmployeeService.update_employee(
                    employee,
                    full_name=form.cleaned_data.get("full_name"),
                    email=form.cleaned_data.get("email"),
                    department=form.cleaned_data.get("department"),
                    role=form.cleaned_data.get("role"),
                    status=form.cleaned_data.get("status"),
                    is_active=form.cleaned_data.get("is_active"),
                    updated_by=request.user
                )
                
                # Handle password reset
                if form.cleaned_data.get("password"):
                    employee.set_password(form.cleaned_data["password"])
                    employee.save()
                
                messages.success(
                    request,
                    "Employee updated successfully.",
                )
                return redirect("employee_detail", employee_id=employee.id)
            except Exception as e:
                messages.error(request, f"Error updating employee: {str(e)}")
    else:
        form = EmployeeForm(
            instance=employee,
            current_user=request.user,
            can_edit_role=role_edit_allowed,
        )

    return render(
        request,
        "employee_management/employee_form.html",
        {
            "form": form,
            "employee": employee,
            "page_title": "Edit Employee",
            "submit_label": "Save Changes",
        },
    )


# ============================================================================
# EMPLOYEE ACTIONS
# ============================================================================

@login_required
@employee_access_required
@require_POST
def employee_activate(request, employee_id):
    """Activate an employee."""
    
    employee = get_employee_or_404(request.user, employee_id)

    if not can_manage_employee(request.user, employee):
        messages.error(request, "You do not have permission to manage this employee.")
        return redirect("employee_list")

    try:
        EmployeeService.activate_employee(employee, request.user)
        messages.success(request, f"{employee.full_name} has been activated.")
    except Exception as e:
        messages.error(request, f"Error activating employee: {str(e)}")

    return redirect(request.POST.get("next", "employee_detail"), employee_id=employee.id)


@login_required
@employee_access_required
@require_POST
def employee_deactivate(request, employee_id):
    """Deactivate an employee."""
    
    employee = get_employee_or_404(request.user, employee_id)

    if employee.id == request.user.id:
        messages.error(request, "You cannot deactivate yourself.")
        return redirect("employee_list")

    if not can_manage_employee(request.user, employee):
        messages.error(request, "You do not have permission to manage this employee.")
        return redirect("employee_list")

    try:
        EmployeeService.deactivate_employee(employee, request.user)
        messages.success(request, f"{employee.full_name} has been deactivated.")
    except Exception as e:
        messages.error(request, f"Error deactivating employee: {str(e)}")

    return redirect(request.POST.get("next", "employee_detail"), employee_id=employee.id)


@login_required
@employee_access_required
@require_POST
def employee_approve(request, employee_id):
    """Approve a pending employee."""
    
    employee = get_employee_or_404(request.user, employee_id)

    if not can_manage_employee(request.user, employee):
        messages.error(request, "You do not have permission to manage this employee.")
        return redirect("employee_list")

    try:
        notes = request.POST.get("notes", "")
        EmployeeService.approve_employee(employee, request.user, notes)
        messages.success(request, f"{employee.full_name} has been approved.")
    except Exception as e:
        messages.error(request, f"Error approving employee: {str(e)}")

    return redirect(request.POST.get("next", "employee_detail"), employee_id=employee.id)


@login_required
@employee_access_required
@require_POST
def employee_reject(request, employee_id):
    """Reject a pending employee."""
    
    employee = get_employee_or_404(request.user, employee_id)

    if not can_manage_employee(request.user, employee):
        messages.error(request, "You do not have permission to manage this employee.")
        return redirect("employee_list")

    try:
        notes = request.POST.get("notes", "")
        EmployeeService.reject_employee(employee, request.user, notes)
        messages.success(request, f"{employee.full_name} has been rejected.")
    except Exception as e:
        messages.error(request, f"Error rejecting employee: {str(e)}")

    return redirect(request.POST.get("next", "employee_detail"), employee_id=employee.id)


@login_required
@employee_access_required
@require_POST
def employee_reset_password(request, employee_id):
    """Reset an employee's password by admin."""
    
    employee = get_employee_or_404(request.user, employee_id)

    if not can_manage_employee(request.user, employee):
        messages.error(request, "You do not have permission to manage this employee.")
        return redirect("employee_list")

    try:
        new_password = EmployeeService.reset_password_by_admin(
            employee, 
            request.user
        )
        messages.success(
            request,
            f"Password reset for {employee.full_name}. New password: {new_password}"
        )
    except Exception as e:
        messages.error(request, f"Error resetting password: {str(e)}")

    return redirect(request.POST.get("next", "employee_detail"), employee_id=employee.id)


# ============================================================================
# BULK ACTIONS
# ============================================================================

@login_required
@employee_access_required
@require_POST
def bulk_action(request):
    """Perform bulk actions on multiple employees."""
    
    if not can_manage_employees(request.user):
        messages.error(request, "You do not have permission for bulk actions.")
        return redirect("employee_list")

    action = request.POST.get("action", "").strip()
    employee_ids = request.POST.getlist("employee_ids", [])
    
    if not employee_ids or not action:
        messages.error(request, "Please select employees and an action.")
        return redirect("employee_list")

    employees = get_visible_employees(request.user).filter(id__in=employee_ids)
    success_count = 0

    try:
        if action == "activate":
            for employee in employees:
                if can_manage_employee(request.user, employee) and not employee.is_active:
                    EmployeeService.activate_employee(employee, request.user)
                    success_count += 1

        elif action == "deactivate":
            for employee in employees:
                if employee.id != request.user.id and can_manage_employee(request.user, employee) and employee.is_active:
                    EmployeeService.deactivate_employee(employee, request.user)
                    success_count += 1

        elif action == "approve":
            for employee in employees:
                if can_manage_employee(request.user, employee) and employee.status == "PENDING":
                    EmployeeService.approve_employee(employee, request.user)
                    success_count += 1

        elif action == "reject":
            for employee in employees:
                if can_manage_employee(request.user, employee) and employee.status == "PENDING":
                    EmployeeService.reject_employee(employee, request.user)
                    success_count += 1

        if success_count > 0:
            messages.success(request, f"Bulk action completed. {success_count} employees updated.")
        else:
            messages.info(request, "No employees were updated.")

    except Exception as e:
        messages.error(request, f"Error performing bulk action: {str(e)}")

    return redirect("employee_list")


# ============================================================================
# APPROVAL CENTER
# ============================================================================

@login_required
@employee_access_required
def approval_center(request):
    """Display approval center for pending employees."""
    
    if request.user.role not in ("SUPER_ADMIN", "ADMIN", "DEPARTMENT_ADMIN"):
        messages.error(request, "You do not have access to the approval center.")
        return redirect("employee_list")

    # Get pending approvals
    pending_approvals = EmployeeApprovalQueue.objects.filter(
        is_approved=False
    ).select_related("employee").order_by("-submitted_at")

    # Filter by department for DEPARTMENT_ADMIN
    if request.user.role == "DEPARTMENT_ADMIN":
        pending_approvals = pending_approvals.filter(
            employee__department=request.user.department
        )

    # Search
    search = request.GET.get("search", "").strip()
    if search:
        pending_approvals = pending_approvals.filter(
            Q(employee__full_name__icontains=search) |
            Q(employee__email__icontains=search)
        )

    # Priority filter
    priority = request.GET.get("priority", "").strip()
    if priority:
        pending_approvals = pending_approvals.filter(priority=priority)

    # Pagination
    page = request.GET.get("page", 1)
    paginator = Paginator(pending_approvals, 20)
    
    try:
        approvals_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        approvals_page = paginator.page(1)

    context = {
        "approvals": approvals_page,
        "paginator": paginator,
        "pending_count": paginator.count,
        "search": search,
        "priority": priority,
        "priorities": EmployeeApprovalQueue.PRIORITY_CHOICES
    }

    return render(
        request,
        "employee_management/approval_center.html",
        context
    )


# ============================================================================
# EXPORT
# ============================================================================

@login_required
@employee_access_required
def export_employees(request):
    """Export employees to CSV."""
    
    if not can_manage_employees(request.user):
        messages.error(request, "You do not have permission to export employees.")
        return redirect("employee_list")

    employees = get_visible_employees(request.user).order_by("full_name")

    # Apply same filters as list view
    search = request.GET.get("search", "").strip()
    role = request.GET.get("role", "").strip()
    status = request.GET.get("status", "").strip()
    department = request.GET.get("department", "").strip()
    active = request.GET.get("active", "").strip()

    if search:
        employees = employees.filter(
            Q(full_name__icontains=search) |
            Q(email__icontains=search) |
            Q(department__icontains=search)
        )

    if role:
        employees = employees.filter(role=role)

    if status:
        employees = employees.filter(status=status)

    if department:
        employees = employees.filter(department=department)

    if active == "yes":
        employees = employees.filter(is_active=True)
    elif active == "no":
        employees = employees.filter(is_active=False)

    # Generate CSV
    try:
        csv_data = EmployeeExportService.export_employees_to_csv(employees)
        
        response = HttpResponse(csv_data, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="employees_export.csv"'
        
        # Log the export
        EmployeeActivityLog.objects.create(
            employee=request.user,
            activity_type="EXPORT",
            performed_by=request.user,
            description=f"Exported {employees.count()} employees"
        )
        
        return response
    except Exception as e:
        messages.error(request, f"Error exporting employees: {str(e)}")
        return redirect("employee_list")


# ============================================================================
# ACTIVITY HISTORY
# ============================================================================

@login_required
@employee_access_required
def activity_history(request, employee_id):
    """View activity history for an employee."""
    
    employee = get_employee_or_404(request.user, employee_id)
    
    activity_logs = EmployeeActivityLog.objects.filter(
        employee=employee
    ).order_by("-created_at")

    # Filter by activity type
    activity_type = request.GET.get("type", "").strip()
    if activity_type:
        activity_logs = activity_logs.filter(activity_type=activity_type)

    # Pagination
    page = request.GET.get("page", 1)
    paginator = Paginator(activity_logs, 50)
    
    try:
        logs_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        logs_page = paginator.page(1)

    context = {
        "employee": employee,
        "activity_logs": logs_page,
        "paginator": paginator,
        "activity_type": activity_type,
        "activity_types": EmployeeActivityLog.ACTIVITY_TYPES
    }

    return render(
        request,
        "employee_management/activity_history.html",
        context
    )


# ============================================================================
# LOGIN HISTORY
# ============================================================================

@login_required
@employee_access_required
def login_history(request, employee_id):
    """View login history for an employee."""
    
    employee = get_employee_or_404(request.user, employee_id)
    
    login_logs = EmployeeLoginHistory.objects.filter(
        employee=employee
    ).order_by("-login_at")

    # Filter by active/inactive sessions
    session_status = request.GET.get("status", "").strip()
    if session_status == "active":
        login_logs = login_logs.filter(is_active=True)
    elif session_status == "inactive":
        login_logs = login_logs.filter(is_active=False)

    active_session_count = login_logs.filter(is_active=True).count()

    # Pagination
    page = request.GET.get("page", 1)
    paginator = Paginator(login_logs, 50)
    
    try:
        logs_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        logs_page = paginator.page(1)

    context = {
        "employee": employee,
        "login_logs": logs_page,
        "paginator": paginator,
        "session_status": session_status,
        "active_session_count": active_session_count,
    }

    return render(
        request,
        "employee_management/login_history.html",
        context
    )


# ============================================================================
# DEPRECATED - KEPT FOR BACKWARD COMPATIBILITY
# ============================================================================

@login_required
@employee_access_required
@require_POST
def employee_status_action(request, employee_id, action):
    """
    DEPRECATED: Use specific action views instead.
    Kept for backward compatibility.
    """
    
    employee = get_employee_or_404(request.user, employee_id)

    if not can_manage_employee(request.user, employee):
        messages.error(request, "You do not have permission to manage this employee.")
        return redirect("employee_list")

    action_mapping = {
        "activate": employee_activate,
        "deactivate": employee_deactivate,
        "approve": employee_approve,
        "reject": employee_reject,
    }

    if action not in action_mapping:
        messages.error(request, "Unknown employee action.")
        return redirect("employee_list")

    # Convert to new action views
    request.POST = request.POST.copy()
    request.POST["next"] = request.POST.get("next", "employee_list")
    
    return action_mapping[action](request, employee_id)


@login_required
@employee_access_required
@require_POST
def employee_delete(request, employee_id):
    """Delete an employee permanently, but only if they are inactive."""
    employee = get_employee_or_404(request.user, employee_id)

    if employee.id == request.user.id:
        messages.error(request, "You cannot delete yourself.")
        return redirect("employee_list")

    if not can_manage_employee(request.user, employee):
        messages.error(request, "You do not have permission to manage this employee.")
        return redirect("employee_list")

    if employee.is_active:
        messages.error(request, "Only inactive employees can be deleted. Please deactivate the employee first.")
        return redirect("employee_detail", employee_id=employee.id)

    try:
        name = employee.full_name
        employee.delete()
        messages.success(request, f"Employee {name} has been permanently deleted.")
        return redirect("employee_list")
    except Exception as e:
        messages.error(request, f"Error deleting employee: {str(e)}")
        return redirect("employee_detail", employee_id=employee.id)


@login_required
@employee_access_required
def global_activity_logs(request):
    """View all activity logs across all employees (Admin & Super Admin only)."""
    if request.user.role not in ["SUPER_ADMIN", "ADMIN"]:
        messages.error(request, "You do not have permission to view global activity logs.")
        return redirect("employee_list")

    activity_logs = EmployeeActivityLog.objects.all().select_related("employee", "performed_by").order_by("-created_at")

    # Filters
    employee_id = request.GET.get("employee", "").strip()
    activity_type = request.GET.get("type", "").strip()

    if employee_id:
        activity_logs = activity_logs.filter(employee_id=employee_id)
    if activity_type:
        activity_logs = activity_logs.filter(activity_type=activity_type)

    # Pagination
    page = request.GET.get("page", 1)
    paginator = Paginator(activity_logs, 50)
    try:
        logs_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        logs_page = paginator.page(1)

    # Get list of employees for the dropdown filter and optimize query
    from tasks.models import ActivityLog as TaskActivityLog
    from employee_management.models import EmployeeLoginHistory
    from django.db.models import OuterRef, Subquery

    # Subquery to get last login time
    last_login_sub = EmployeeLoginHistory.objects.filter(employee=OuterRef('pk')).order_by('-login_at').values('login_at')[:1]
    
    # Subqueries to get last task time and action
    last_task_time_sub = TaskActivityLog.objects.filter(user=OuterRef('pk')).order_by('-timestamp').values('timestamp')[:1]
    last_task_action_sub = TaskActivityLog.objects.filter(user=OuterRef('pk')).order_by('-timestamp').values('action')[:1]

    # Annotate employees with these values in ONE query!
    employees = EmployeeUser.objects.annotate(
        last_login_time=Subquery(last_login_sub),
        last_task_time=Subquery(last_task_time_sub),
        last_task_action=Subquery(last_task_action_sub)
    ).order_by("full_name")

    # Populate employees_activity summary list
    employees_activity = [
        {
            "employee": emp,
            "last_login": emp.last_login_time,
            "last_task_time": emp.last_task_time,
            "last_task_action": emp.last_task_action,
        }
        for emp in employees
    ]

    context = {
        "activity_logs": logs_page,
        "paginator": paginator,
        "employees": employees,
        "selected_employee": employee_id,
        "selected_type": activity_type,
        "activity_types": EmployeeActivityLog.ACTIVITY_TYPES,
        "employees_activity": employees_activity,
    }

    return render(
        request,
        "employee_management/global_activity_logs.html",
        context
    )


