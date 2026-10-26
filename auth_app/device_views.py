import logging
import uuid
from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from .models import AuthorizedDevice, EmployeeUser
from .middleware import get_client_ip

logger = logging.getLogger(__name__)


def parse_user_agent(ua_string):
    """
    Parse a user agent string to identify OS, browser, and device classification.
    Helps administrators in India immediately verify whether the request originated
    from a company laptop or an unauthorized mobile/tablet.
    """
    if not ua_string:
        return "Unknown Device"

    ua_lower = ua_string.lower()

    # Device type detection
    is_mobile = any(keyword in ua_lower for keyword in ["iphone", "android", "mobile", "phone"])
    is_tablet = any(keyword in ua_lower for keyword in ["ipad", "tablet"])

    device_type = "Desktop / Laptop"
    if is_tablet:
        device_type = "Tablet"
    elif is_mobile:
        device_type = "Smartphone / Mobile"

    # OS detection
    os_name = "Unknown OS"
    if "windows nt 10.0" in ua_lower or "windows nt 11.0" in ua_lower:
        os_name = "Windows 10/11"
    elif "windows" in ua_lower:
        os_name = "Windows"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        os_name = "macOS"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "iphone" in ua_lower or "ipad" in ua_lower:
        os_name = "iOS"
    elif "cros" in ua_lower:
        os_name = "Chrome OS"
    elif "linux" in ua_lower:
        os_name = "Linux"

    # Browser detection
    browser_name = "Unknown Browser"
    if "edg/" in ua_lower:
        browser_name = "Microsoft Edge"
    elif "chrome/" in ua_lower and "safari/" in ua_lower and "edg/" not in ua_lower:
        browser_name = "Google Chrome"
    elif "firefox/" in ua_lower:
        browser_name = "Mozilla Firefox"
    elif "safari/" in ua_lower and "chrome/" not in ua_lower:
        browser_name = "Apple Safari"
    elif "opr/" in ua_lower or "opera" in ua_lower:
        browser_name = "Opera"

    return f"{device_type} • {os_name} • {browser_name}"


def device_register_view(request):
    """
    Displays company device registration form for employees.
    Employees register their laptop/desktop for approval by Management in India.
    """
    cookie_name = getattr(settings, "DEVICE_COOKIE_NAME", "ff_device_token")
    pending_cookie_name = getattr(settings, "DEVICE_PENDING_COOKIE_NAME", "ff_pending_device_token")

    # If already approved, redirect to login
    token_str = request.COOKIES.get(cookie_name)
    if token_str:
        try:
            token_uuid = uuid.UUID(str(token_str).strip())
            approved_device = AuthorizedDevice.objects.filter(device_token=token_uuid, status="APPROVED").first()
            if approved_device:
                return redirect("/login/")
        except (ValueError, TypeError):
            pass

    # If pending cookie exists, direct to pending page
    pending_token = request.COOKIES.get(pending_cookie_name) or request.session.get("pending_device_token")
    if pending_token and request.method == "GET":
        try:
            pending_uuid = uuid.UUID(str(pending_token).strip())
            pending_device = AuthorizedDevice.objects.filter(device_token=pending_uuid, status="PENDING").first()
            if pending_device:
                return redirect("/device/pending/")
        except (ValueError, TypeError):
            pass

    # Fetch departments for select dropdown if available
    departments = []
    try:
        from employee_management.models import Department
        departments = Department.objects.all().order_by("name")
    except Exception:
        pass

    if request.method == "POST":
        requested_by_name = request.POST.get("requested_by_name", "").strip()
        employee_email = request.POST.get("employee_email", "").strip()
        department = request.POST.get("department", "").strip()
        device_name = request.POST.get("device_name", "").strip()
        asset_tag = request.POST.get("asset_tag", "").strip()

        errors = []
        if not requested_by_name:
            errors.append("Please enter your full name.")
        if not device_name:
            errors.append("Please enter your laptop model or device name.")

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(
                request,
                "device_management/register.html",
                {
                    "departments": departments,
                    "requested_by_name": requested_by_name,
                    "employee_email": employee_email,
                    "department": department,
                    "device_name": device_name,
                    "asset_tag": asset_tag,
                },
            )

        client_ip = get_client_ip(request)
        ua_raw = request.META.get("HTTP_USER_AGENT", "")
        ua_summary = parse_user_agent(ua_raw)

        # Attempt to auto-link assigned employee profile if email is registered
        assigned_user = None
        if employee_email:
            assigned_user = EmployeeUser.objects.filter(email__iexact=employee_email).first()

        device = AuthorizedDevice.objects.create(
            device_token=uuid.uuid4(),
            device_name=device_name,
            asset_tag=asset_tag or None,
            requested_by_name=requested_by_name,
            employee_email=employee_email or None,
            department=department,
            assigned_user=assigned_user,
            status="PENDING",
            registration_ip=client_ip,
            last_seen_ip=client_ip,
            user_agent_summary=ua_summary,
        )

        logger.info(
            f"New device registration created: ID={device.id}, Name='{device_name}', "
            f"User='{requested_by_name}', IP={client_ip}"
        )

        response = redirect("/device/pending/")
        secure = request.is_secure() or (not settings.DEBUG)

        # Store in session and set pending cookie (30 days)
        request.session["pending_device_token"] = str(device.device_token)
        response.set_cookie(
            key=pending_cookie_name,
            value=str(device.device_token),
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            samesite="Lax",
            secure=secure,
        )
        return response

    client_ip = get_client_ip(request)
    detected_device = parse_user_agent(request.META.get("HTTP_USER_AGENT", ""))

    return render(
        request,
        "device_management/register.html",
        {
            "departments": departments,
            "client_ip": client_ip,
            "detected_device": detected_device,
        },
    )


def device_pending_view(request):
    """
    Displays the waiting screen while Management in India reviews the registration.
    Runs automated JavaScript polling to /device/status-check/ every 5 seconds.
    Auto-redirects to login once approved.
    """
    pending_cookie_name = getattr(settings, "DEVICE_PENDING_COOKIE_NAME", "ff_pending_device_token")
    cookie_name = getattr(settings, "DEVICE_COOKIE_NAME", "ff_device_token")

    token_str = (
        request.COOKIES.get(pending_cookie_name)
        or request.session.get("pending_device_token")
        or request.GET.get("token")
    )

    if not token_str:
        return redirect("/device/register/")

    try:
        token_uuid = uuid.UUID(str(token_str).strip())
    except (ValueError, TypeError):
        return redirect("/device/register/")

    device = AuthorizedDevice.objects.filter(device_token=token_uuid).first()
    if not device:
        return redirect("/device/register/")

    # If already approved, issue permanent cookie and redirect to login
    if device.status == "APPROVED":
        messages.success(request, f"Device '{device.device_name}' has been approved! You may now sign in.")
        response = redirect("/login/")
        secure = request.is_secure() or (not settings.DEBUG)
        cookie_age = getattr(settings, "DEVICE_COOKIE_AGE", 365 * 24 * 60 * 60)

        response.set_cookie(
            key=cookie_name,
            value=str(device.device_token),
            max_age=cookie_age,
            httponly=True,
            samesite="Lax",
            secure=secure,
        )
        response.delete_cookie(pending_cookie_name)
        return response

    if device.status in ("REJECTED", "REVOKED"):
        return redirect("/device/unauthorized/")

    return render(
        request,
        "device_management/pending.html",
        {
            "device": device,
            "token": str(device.device_token),
            "client_ip": get_client_ip(request),
        },
    )


def device_status_check_view(request):
    """
    AJAX endpoint called by the pending screen every 5 seconds.
    Returns JSON status. If APPROVED, attaches the permanent ff_device_token cookie.
    """
    pending_cookie_name = getattr(settings, "DEVICE_PENDING_COOKIE_NAME", "ff_pending_device_token")
    cookie_name = getattr(settings, "DEVICE_COOKIE_NAME", "ff_device_token")

    token_str = (
        request.COOKIES.get(pending_cookie_name)
        or request.COOKIES.get(cookie_name)
        or request.session.get("pending_device_token")
        or request.GET.get("token")
    )

    if not token_str:
        return JsonResponse({"status": "UNKNOWN", "message": "No token provided."}, status=400)

    try:
        token_uuid = uuid.UUID(str(token_str).strip())
    except (ValueError, TypeError):
        return JsonResponse({"status": "INVALID", "message": "Invalid token format."}, status=400)

    device = AuthorizedDevice.objects.filter(device_token=token_uuid).first()
    if not device:
        return JsonResponse({"status": "NOT_FOUND", "message": "Device not registered."}, status=404)

    if device.status == "APPROVED":
        response = JsonResponse({
            "status": "APPROVED",
            "device_name": device.device_name,
            "redirect_url": "/login/",
        })
        secure = request.is_secure() or (not settings.DEBUG)
        cookie_age = getattr(settings, "DEVICE_COOKIE_AGE", 365 * 24 * 60 * 60)

        response.set_cookie(
            key=cookie_name,
            value=str(device.device_token),
            max_age=cookie_age,
            httponly=True,
            samesite="Lax",
            secure=secure,
        )
        response.delete_cookie(pending_cookie_name)
        return response

    elif device.status == "REJECTED":
        return JsonResponse({
            "status": "REJECTED",
            "redirect_url": "/device/unauthorized/",
            "message": "Device registration was rejected by administrator.",
        })

    elif device.status == "REVOKED":
        return JsonResponse({
            "status": "REVOKED",
            "redirect_url": "/device/unauthorized/",
            "message": "Device access has been revoked.",
        })

    return JsonResponse({
        "status": "PENDING",
        "device_name": device.device_name,
        "requested_by": device.requested_by_name,
    })


def device_unauthorized_view(request):
    """
    Displayed when access is attempted from an unauthorized, revoked, or rejected device.
    Provides clear, professional IT support guidance.
    """
    cookie_name = getattr(settings, "DEVICE_COOKIE_NAME", "ff_device_token")
    pending_cookie_name = getattr(settings, "DEVICE_PENDING_COOKIE_NAME", "ff_pending_device_token")

    token_str = request.COOKIES.get(cookie_name) or request.COOKIES.get(pending_cookie_name)
    device = None
    if token_str:
        try:
            token_uuid = uuid.UUID(str(token_str).strip())
            device = AuthorizedDevice.objects.filter(device_token=token_uuid).first()
        except (ValueError, TypeError):
            pass

    client_ip = get_client_ip(request)
    ua_summary = parse_user_agent(request.META.get("HTTP_USER_AGENT", ""))

    response = render(
        request,
        "device_management/unauthorized.html",
        {
            "device": device,
            "client_ip": client_ip,
            "detected_device": ua_summary,
            "now": timezone.now(),
        },
    )
    # Clear device cookie on unauthorized view
    response.delete_cookie(cookie_name)
    return response
