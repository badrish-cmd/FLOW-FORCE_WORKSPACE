import logging
import uuid
from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone
from .models import AuthorizedDevice

logger = logging.getLogger(__name__)


def get_client_ip(request):
    """
    Extract the client's real IP address from HTTP headers (handling reverse proxy).
    """
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class DeviceGatekeeperMiddleware:
    """
    FlowForce Gatekeeper Middleware.
    Restricts access exclusively to authorized company hardware.
    Checks for the 'ff_device_token' cookie and validates against AuthorizedDevice records.
    """

    EXEMPT_PATH_PREFIXES = (
        "/device/",
        "/static/",
        "/media/",
        "/favicon.ico",
        "/admin/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # 1. Skip check if running tests and testing flag is not enabled
        is_testing = getattr(settings, "IS_TESTING", False)
        device_restriction_testing = getattr(settings, "DEVICE_RESTRICTION_TESTING", False)
        if is_testing and not device_restriction_testing:
            return self.get_response(request)

        # 2. Check operational mode: ENFORCE | LOG_ONLY | DISABLED
        mode = getattr(settings, "DEVICE_RESTRICTION_MODE", "ENFORCE").upper()
        if mode == "DISABLED":
            return self.get_response(request)

        # 3. Exempt path filtering
        for prefix in self.EXEMPT_PATH_PREFIXES:
            if path.startswith(prefix) or path == prefix:
                return self.get_response(request)

        # 4. SuperAdmin & Emergency Bypass
        # Check bypass token in headers or query parameters if configured
        bypass_token = getattr(settings, "DEVICE_BYPASS_TOKEN", None)
        if bypass_token:
            req_token = request.headers.get("X-Device-Bypass-Token") or request.GET.get("bypass_token")
            if req_token and req_token == bypass_token:
                logger.info("Device gatekeeper bypassed via DEVICE_BYPASS_TOKEN.")
                return self.get_response(request)

        # If user is authenticated as SUPER_ADMIN or superuser, allow pass
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            if getattr(user, "role", None) == "SUPER_ADMIN" or getattr(user, "is_superuser", False):
                return self.get_response(request)

        cookie_name = getattr(settings, "DEVICE_COOKIE_NAME", "ff_device_token")
        pending_cookie_name = getattr(settings, "DEVICE_PENDING_COOKIE_NAME", "ff_pending_device_token")
        token_str = request.COOKIES.get(cookie_name)

        # 5. Soft Mode / Log-Only handling
        if mode in ("LOG_ONLY", "SOFT"):
            if token_str:
                try:
                    dev_uuid = uuid.UUID(str(token_str).strip())
                    device = AuthorizedDevice.objects.filter(device_token=dev_uuid).first()
                    if device:
                        request.authorized_device = device
                except (ValueError, TypeError):
                    pass
            logger.debug(f"[DeviceGatekeeper:LogOnly] Path={path}, IP={get_client_ip(request)}, Token={token_str}")
            return self.get_response(request)

        # 6. ENFORCE MODE: Cookie Verification
        if not token_str:
            # Check if pending registration cookie exists
            if request.COOKIES.get(pending_cookie_name):
                return redirect("/device/pending/")
            return redirect("/device/register/")

        # Validate UUID format
        try:
            device_token = uuid.UUID(str(token_str).strip())
        except (ValueError, TypeError):
            # Corrupted cookie -> clear and redirect
            response = redirect("/device/register/")
            response.delete_cookie(cookie_name)
            return response

        # 7. Database Verification
        device = AuthorizedDevice.objects.filter(device_token=device_token).first()
        if not device:
            # Token not recognized in database -> delete cookie & redirect to register
            response = redirect("/device/register/")
            response.delete_cookie(cookie_name)
            return response

        # Check status
        if device.status == "APPROVED":
            # Throttled update of last_seen_ip and last_used_at (avoid heavy DB writes on every asset request)
            client_ip = get_client_ip(request)
            now = timezone.now()
            time_diff = (now - device.last_used_at).total_seconds() if device.last_used_at else 9999
            if time_diff > 300 or device.last_seen_ip != client_ip:
                AuthorizedDevice.objects.filter(id=device.id).update(
                    last_used_at=now,
                    last_seen_ip=client_ip,
                )

            request.authorized_device = device
            return self.get_response(request)

        elif device.status == "PENDING":
            return redirect("/device/pending/")

        elif device.status in ("REJECTED", "REVOKED"):
            response = redirect("/device/unauthorized/")
            response.delete_cookie(cookie_name)
            return response

        # Fallback redirect to register
        return redirect("/device/register/")
