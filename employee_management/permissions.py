from django.contrib import messages
from django.shortcuts import redirect


MANAGER_ROLES = (
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


def can_view_employees(user):
    return (
        user.is_authenticated
        and user.role in VIEWER_ROLES
    )


def can_manage_employees(user):
    return (
        user.is_authenticated
        and user.role in MANAGER_ROLES
    )


def can_manage_employee(user, employee):
    if not user.is_authenticated:
        return False

    if user.role == "SUPER_ADMIN":
        return True

    if user.role == "ADMIN":
        return employee.role != "SUPER_ADMIN"

    if user.role == "DEPARTMENT_ADMIN":
        return (
            employee.role == "EMPLOYEE"
            and employee.department == user.department
        )

    return False


def can_edit_employee_role(user, employee):
    if not user.is_authenticated:
        return False

    if user.role == "SUPER_ADMIN":
        return True

    if user.role == "ADMIN":
        return employee.role != "SUPER_ADMIN"

    if user.role == "DEPARTMENT_ADMIN":
        return (
            employee.role == "EMPLOYEE"
            and employee.department == user.department
        )

    return False


def employee_access_required(view_func):
    def wrapped(request, *args, **kwargs):
        if not can_view_employees(request.user):
            messages.error(
                request,
                "You do not have access to Employee Management."
            )
            return redirect("employee_dashboard")

        return view_func(request, *args, **kwargs)

    return wrapped


def employee_manage_required(view_func):
    def wrapped(request, *args, **kwargs):
        if not can_manage_employees(request.user):
            messages.error(
                request,
                "You do not have permission to manage employees."
            )
            return redirect("employee_list")

        return view_func(request, *args, **kwargs)

    return wrapped
