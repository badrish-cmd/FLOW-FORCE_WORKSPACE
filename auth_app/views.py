from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth import login
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils import timezone

from .models import EmployeeUser
from .models import PasswordResetOTP

from .services import generate_otp
from .services import send_otp_email
from task_tracker.services import get_dashboard_stats_for_user


def home_view(request):
    if request.user.is_authenticated:
        return redirect(get_role_redirect_name(request.user))
    return redirect("login")


def login_view(request):

    print("LOGIN VIEW HIT")
    print("METHOD:", request.method)

    if request.method == "POST":

        print("POST:", request.POST)

        email = request.POST.get(
            "email",
            ""
        ).strip().lower()

        password = request.POST.get(
            "password"
        )

        user = authenticate(
            request,
            username=email,
            password=password
        )

        print("EMAIL:", email)
        print("AUTH USER:", user)

        if user is None:

            try:

                user = EmployeeUser.objects.get(
                    email=email
                )

            except EmployeeUser.DoesNotExist:

                user = None

            if (
                user is None
                or not user.check_password(password)
            ):

                messages.error(
                    request,
                    "Invalid email or password."
                )

                return redirect(
                    "login"
                )

        if user.status == "PENDING":

            messages.error(
                request,
                "Your account is awaiting approval."
            )

            return redirect(
                "login"
            )

        if user.status == "REJECTED":

            messages.error(
                request,
                "Your account has been rejected."
            )

            return redirect(
                "login"
            )

        if not user.is_active:

            messages.error(
                request,
                "Account disabled."
            )

            return redirect(
                "login"
            )

        login(
            request,
            user
        )

        # Log the login activity
        from employee_management.services import EmployeeService
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        ip = x_forwarded_for.split(',')[0] if x_forwarded_for else request.META.get('REMOTE_ADDR', '')
        ua = request.META.get('HTTP_USER_AGENT', '')
        if not request.session.session_key:
            request.session.save()
        EmployeeService.log_login(user, ip_address=ip, user_agent=ua, session_key=request.session.session_key or "")

        # Keep session logged in forever (10 years) until logged out
        from django.conf import settings
        request.session.set_expiry(settings.SESSION_COOKIE_AGE)

        request.session.modified = True

        return redirect(
            get_role_redirect_name(
                user
            )
        )

    return render(
        request,
        "auth/login.html"
    )


def get_role_redirect_name(user):

    role_redirects = {
        "SUPER_ADMIN": "super_admin_dashboard",
        "ADMIN": "admin_dashboard",
        "DEPARTMENT_ADMIN": "department_dashboard",
        "EMPLOYEE": "employee_dashboard",
    }

    return role_redirects.get(
        user.role,
        "employee_dashboard"
    )


def redirect_if_wrong_role(request, role):

    if request.user.role != role:

        return redirect(
            get_role_redirect_name(
                request.user
            )
        )

    return None


@login_required
def super_admin_dashboard(request):

    role_redirect = redirect_if_wrong_role(
        request,
        "SUPER_ADMIN"
    )

    if role_redirect:

        return role_redirect

    from tables.models import Table
    context = {
        "total_employees": EmployeeUser.objects.count(),
        "total_tables": Table.objects.filter(is_active=True).count(),
    }

    return render(
        request,
        "dashboard/super_admin.html",
        context
    )


@login_required
def admin_dashboard(request):

    role_redirect = redirect_if_wrong_role(
        request,
        "ADMIN"
    )

    if role_redirect:

        return role_redirect

    from tables.models import Table
    context = {
        "total_employees": EmployeeUser.objects.count(),
        "total_tables": Table.objects.filter(is_active=True).count(),
    }

    return render(
        request,
        "dashboard/admin.html",
        context
    )


@login_required
def department_dashboard(request):

    role_redirect = redirect_if_wrong_role(
        request,
        "DEPARTMENT_ADMIN"
    )

    if role_redirect:

        return role_redirect

    from tables.models import Table
    context = {
        "total_employees": EmployeeUser.objects.count(),
        "total_tables": Table.objects.filter(is_active=True).count(),
    }

    return render(
        request,
        "dashboard/department_admin.html",
        context
    )


@login_required
def employee_dashboard(request):

    role_redirect = redirect_if_wrong_role(
        request,
        "EMPLOYEE"
    )

    if role_redirect:

        return role_redirect

    from tables.models import Table
    context = {
        "total_employees": EmployeeUser.objects.count(),
        "total_tables": Table.objects.filter(is_active=True).count(),
    }

    return render(
        request,
        "dashboard/employee.html",
        context
    )


def logout_view(request):

    if request.user.is_authenticated:
        from employee_management.services import EmployeeService
        EmployeeService.log_logout(request.user)

    logout(
        request
    )

    return redirect(
        "login"
    )


def forgot_password_view(request):

    if request.method == "POST":

        email = request.POST.get(
            "email"
        )

        try:

            user = EmployeeUser.objects.get(
                email=email
            )

            otp = generate_otp()

            PasswordResetOTP.objects.create(
                user=user,
                otp_code=otp
            )

            send_otp_email(
                user,
                otp
            )

            request.session[
                "reset_email"
            ] = email

            return redirect(
                "verify_otp"
            )

        except EmployeeUser.DoesNotExist:

            return render(
                request,
                "auth/forgot_password.html",
                {
                    "error":
                    "Email not found."
                }
            )

    return render(
        request,
        "auth/forgot_password.html"
    )


def verify_otp_view(request):

    if request.method == "POST":

        entered_otp = request.POST.get(
            "otp"
        )

        email = request.session.get(
            "reset_email"
        )

        if not email:

            messages.error(
                request,
                "Session expired."
            )

            return redirect(
                "forgot_password"
            )

        try:

            user = EmployeeUser.objects.get(
                email=email
            )

            otp_record = (
                PasswordResetOTP.objects
                .filter(
                    user=user,
                    is_used=False
                )
                .latest(
                    "created_at"
                )
            )

            if (
                otp_record.expires_at
                < timezone.now()
            ):

                messages.error(
                    request,
                    "OTP expired."
                )

                return redirect(
                    "verify_otp"
                )

            if (
                otp_record.otp_code
                != entered_otp
            ):

                messages.error(
                    request,
                    "Invalid OTP."
                )

                return redirect(
                    "verify_otp"
                )

            request.session[
                "reset_user_id"
            ] = user.id

            messages.success(
                request,
                "OTP verified."
            )

            return redirect(
                "reset_password"
            )

        except Exception:

            messages.error(
                request,
                "OTP verification failed."
            )

    return render(
        request,
        "auth/verify_otp.html"
    )

def reset_password_view(request):

    if request.method == "POST":

        password = request.POST.get(
            "password"
        )

        confirm_password = request.POST.get(
            "confirm_password"
        )

        if password != confirm_password:

            messages.error(
                request,
                "Passwords do not match."
            )

            return redirect(
                "reset_password"
            )

        user_id = request.session.get(
            "reset_user_id"
        )

        if not user_id:

            messages.error(
                request,
                "Session expired."
            )

            return redirect(
                "forgot_password"
            )

        user = EmployeeUser.objects.get(
            id=user_id
        )

        user.set_password(
            password
        )

        user.save()

        PasswordResetOTP.objects.filter(
            user=user,
            is_used=False
        ).update(
            is_used=True
        )

        request.session.flush()

        messages.success(
            request,
            "Password changed successfully."
        )

        return redirect(
            "login"
        )

    return render(
        request,
        "auth/reset_password.html"
    )


def register_view(request):

    if request.method == "POST":

        full_name = request.POST.get(
            "full_name"
        )

        email = request.POST.get(
            "email"
        )

        password = request.POST.get(
            "password"
        )

        confirm_password = request.POST.get(
            "confirm_password"
        )

        email_lower = email.lower().strip() if email else ""
        if not (email_lower.endswith("@flow-force.com") or email_lower.endswith("@flowforceengineering.com")):
            messages.error(
                request,
                "Only @flow-force.com or @flowforceengineering.com emails are allowed."
            )

            return redirect(
                "register"
            )

        if password != confirm_password:

            messages.error(
                request,
                "Passwords do not match."
            )

            return redirect(
                "register"
            )

        if EmployeeUser.objects.filter(
            email=email
        ).exists():

            messages.error(
                request,
                "Email already exists."
            )

            return redirect(
                "register"
            )

        EmployeeUser.objects.create_user(
            email=email,
            password=password,
            full_name=full_name,
            role="EMPLOYEE",
            status="PENDING"
        )

        messages.success(
            request,
            "Registration submitted. Awaiting approval."
        )

        return redirect(
            "login"
        )

    return render(
        request,
        "auth/register.html"
    )
