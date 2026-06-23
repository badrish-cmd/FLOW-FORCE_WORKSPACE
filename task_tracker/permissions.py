from django.contrib import messages
from django.shortcuts import redirect


MANAGER_ROLES = (
    "SUPER_ADMIN",
    "ADMIN",
)

DEPARTMENT_MANAGER_ROLES = (
    "SUPER_ADMIN",
    "ADMIN",
    "DEPARTMENT_ADMIN",
)

VIEWER_ROLES = (
    "SUPER_ADMIN",
    "ADMIN",
    "DEPARTMENT_ADMIN",
    "EMPLOYEE",
)


def can_view_trackers(user):
    return user.is_authenticated and user.role in VIEWER_ROLES


def can_manage_trackers(user):
    return user.is_authenticated and user.role in MANAGER_ROLES


def can_manage_tracker_columns(user, tracker):
    if not user.is_authenticated:
        return False
    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return True
    return False


def can_manage_tasks(user, tracker):
    if not user.is_authenticated:
        return False
    if user.role == "SUPER_ADMIN":
        return True
    if user.role == "ADMIN":
        return True
    if user.role == "DEPARTMENT_ADMIN":
        return tracker.department == user.department
    return False


def can_view_task(user, task):
    if not user.is_authenticated:
        return False
    if user.role == "SUPER_ADMIN":
        return True
    if user.role == "ADMIN":
        return True
    if user.role == "DEPARTMENT_ADMIN":
        return task.tracker.department == user.department
    if user.role == "EMPLOYEE":
        return task.assigned_to_id == user.id
    return False


def can_update_task_status(user, task):
    if not user.is_authenticated:
        return False
    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return True
    if user.role == "DEPARTMENT_ADMIN":
        return task.tracker.department == user.department
    if user.role == "EMPLOYEE":
        return task.assigned_to_id == user.id
    return False


def tracker_access_required(view_func):
    def wrapped(request, *args, **kwargs):
        if not can_view_trackers(request.user):
            messages.error(request, "You do not have access to the task tracker.")
            return redirect("employee_dashboard")
        return view_func(request, *args, **kwargs)

    return wrapped


def tracker_manage_required(view_func):
    def wrapped(request, *args, **kwargs):
        if not can_manage_trackers(request.user):
            messages.error(request, "You do not have permission to manage trackers.")
            return redirect("task_tracker:tracker_dashboard")
        return view_func(request, *args, **kwargs)

    return wrapped
