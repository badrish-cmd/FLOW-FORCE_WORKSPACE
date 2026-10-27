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
    x_real_ip = request.META.get("HTTP_X_REAL_IP")
    if x_real_ip:
        return x_real_ip.strip()
    return request.META.get("REMOTE_ADDR")


class DeviceGatekeeperMiddleware:
    """
    FlowForce Gatekeeper Middleware.
    Restricts access exclusively to authorized company hardware and authorized IP addresses.
    Checks for the 'ff_device_token' cookie and validates against AuthorizedDevice records.
    If accessing from an approved IP, automatically grants access and provisions the device cookie.
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
        cookie_age = getattr(settings, "DEVICE_COOKIE_AGE", 365 * 24 * 60 * 60)
        secure = request.is_secure() or (not settings.DEBUG)

        client_ip = get_client_ip(request)
        ip_is_approved = AuthorizedDevice.is_ip_approved(client_ip)

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
            logger.debug(f"[DeviceGatekeeper:LogOnly] Path={path}, IP={client_ip}, Token={token_str}")
            return self.get_response(request)

        # 6. ENFORCE MODE: Cookie Verification & IP Authorization
        if not token_str:
            # Check if this IP is already approved
            if ip_is_approved:
                approved_device = AuthorizedDevice.get_approved_device_for_ip(client_ip, user=user)
                if not approved_device:
                    approved_device = AuthorizedDevice.objects.create(
                        device_token=uuid.uuid4(),
                        device_name=f"Authorized Network Device ({client_ip})",
                        requested_by_name="Office Network User",
                        status="APPROVED",
                        registration_ip=client_ip,
                        last_seen_ip=client_ip,
                        approved_at=timezone.now(),
                    )

                # Auto-approve pending device cookie if present
                pending_token = request.COOKIES.get(pending_cookie_name)
                if pending_token:
                    try:
                        p_uuid = uuid.UUID(str(pending_token).strip())
                        AuthorizedDevice.objects.filter(device_token=p_uuid, status="PENDING").update(
                            status="APPROVED",
                            approved_at=timezone.now(),
                        )
                    except (ValueError, TypeError):
                        pass

                request.authorized_device = approved_device
                response = self.get_response(request)
                response.set_cookie(
                    key=cookie_name,
                    value=str(approved_device.device_token),
                    max_age=cookie_age,
                    httponly=True,
                    samesite="Lax",
                    secure=secure,
                )
                if request.COOKIES.get(pending_cookie_name):
                    response.delete_cookie(pending_cookie_name)
                return response

            # Check if pending registration cookie exists
            if request.COOKIES.get(pending_cookie_name):
                return redirect("/device/pending/")
            return redirect("/device/register/")

        # Validate UUID format
        try:
            device_token = uuid.UUID(str(token_str).strip())
        except (ValueError, TypeError):
            if ip_is_approved:
                approved_device = AuthorizedDevice.get_approved_device_for_ip(client_ip, user=user)
                if approved_device:
                    request.authorized_device = approved_device
                    response = self.get_response(request)
                    response.set_cookie(
                        key=cookie_name,
                        value=str(approved_device.device_token),
                        max_age=cookie_age,
                        httponly=True,
                        samesite="Lax",
                        secure=secure,
                    )
                    return response
            # Corrupted cookie -> clear and redirect
            response = redirect("/device/register/")
            response.delete_cookie(cookie_name)
            return response

        # 7. Database Verification
        device = AuthorizedDevice.objects.filter(device_token=device_token).first()
        if not device:
            if ip_is_approved:
                approved_device = AuthorizedDevice.get_approved_device_for_ip(client_ip, user=user)
                if approved_device:
                    request.authorized_device = approved_device
                    response = self.get_response(request)
                    response.set_cookie(
                        key=cookie_name,
                        value=str(approved_device.device_token),
                        max_age=cookie_age,
                        httponly=True,
                        samesite="Lax",
                        secure=secure,
                    )
                    return response
            # Token not recognized in database -> delete cookie & redirect to register
            response = redirect("/device/register/")
            response.delete_cookie(cookie_name)
            return response

        # Check status
        if device.status == "APPROVED":
            # Throttled update of last_seen_ip and last_used_at (avoid heavy DB writes on every asset request)
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
            if ip_is_approved:
                # Promote pending device immediately since IP is pre-authorized
                device.status = "APPROVED"
                device.approved_at = timezone.now()
                device.last_seen_ip = client_ip
                device.save(update_fields=["status", "approved_at", "last_seen_ip"])
                request.authorized_device = device
                response = self.get_response(request)
                response.set_cookie(
                    key=cookie_name,
                    value=str(device.device_token),
                    max_age=cookie_age,
                    httponly=True,
                    samesite="Lax",
                    secure=secure,
                )
                if request.COOKIES.get(pending_cookie_name):
                    response.delete_cookie(pending_cookie_name)
                return response
            return redirect("/device/pending/")

        elif device.status in ("REJECTED", "REVOKED"):
            response = redirect("/device/unauthorized/")
            response.delete_cookie(cookie_name)
            return response

        # Fallback redirect to register
        if ip_is_approved:
            return self.get_response(request)
        return redirect("/device/register/")

