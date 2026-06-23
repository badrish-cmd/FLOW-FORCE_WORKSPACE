from django.urls import path

from .views import (
    admin_dashboard,
    department_dashboard,
    employee_dashboard,
    forgot_password_view,
    home_view,
    login_view,
    logout_view,
    register_view,
    reset_password_view,
    super_admin_dashboard,
    verify_otp_view,
)

urlpatterns = [

    path(
        "login/",
        login_view,
        name="login"
    ),

    path(
        "forgot-password/",
        forgot_password_view,
        name="forgot_password"
    ),

    path(
        "verify-otp/",
        verify_otp_view,
        name="verify_otp"
    ),

    path(
        "reset-password/",
        reset_password_view,
        name="reset_password"
    ),

    path(
        "super-admin-dashboard/",
        super_admin_dashboard,
        name="super_admin_dashboard"
    ),

    path(
        "admin-dashboard/",
        admin_dashboard,
        name="admin_dashboard"
    ),

    path(
        "department-dashboard/",
        department_dashboard,
        name="department_dashboard"
    ),

    path(
        "employee-dashboard/",
        employee_dashboard,
        name="employee_dashboard"
    ),

    path(
        "logout/",
        logout_view,
        name="logout"
    ),

    path(
        "register/",
        register_view,
        name="register"
    ),

    path(
        "",
        home_view,
        name="home"
    ),

]
