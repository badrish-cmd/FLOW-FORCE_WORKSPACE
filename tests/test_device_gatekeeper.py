import uuid
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from auth_app.models import EmployeeUser, AuthorizedDevice


class DeviceModelTest(TestCase):
    """Unit tests for AuthorizedDevice database model."""

    def setUp(self):
        self.user = EmployeeUser.objects.create_user(
            email="employee@flow-force.com",
            full_name="Budi Santoso",
            password="securepassword123",
            role="EMPLOYEE",
            status="APPROVED",
        )

    def test_create_authorized_device(self):
        device = AuthorizedDevice.objects.create(
            device_name="Lenovo ThinkPad X1",
            asset_tag="FF-ID-001",
            requested_by_name="Budi Santoso",
            employee_email="employee@flow-force.com",
            assigned_user=self.user,
            department="Operations",
            status="PENDING",
        )
        self.assertIsNotNone(device.id)
        self.assertIsNotNone(device.device_token)
        self.assertEqual(device.status, "PENDING")
        self.assertFalse(device.is_approved)
        self.assertIn("Lenovo ThinkPad X1", str(device))


class DeviceViewsTest(TestCase):
    """Functional tests for device registration, pending, and status polling views."""

    def setUp(self):
        self.client = Client()
        self.user = EmployeeUser.objects.create_user(
            email="staff@flow-force.com",
            full_name="Staff Member",
            password="password123",
            role="EMPLOYEE",
            status="APPROVED",
        )

    def test_register_view_get(self):
        response = self.client.get(reverse("device_register"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Company Device Registration")

    def test_register_view_post_success(self):
        response = self.client.post(reverse("device_register"), {
            "requested_by_name": "Staff Member",
            "employee_email": "staff@flow-force.com",
            "department": "Engineering",
            "device_name": "HP EliteBook 840",
            "asset_tag": "ASSET-999",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/device/pending/")

        # Verify device created in DB
        device = AuthorizedDevice.objects.filter(device_name="HP EliteBook 840").first()
        self.assertIsNotNone(device)
        self.assertEqual(device.status, "PENDING")
        self.assertEqual(device.assigned_user, self.user)

        # Verify pending cookie set
        self.assertIn("ff_pending_device_token", response.cookies)
        self.assertEqual(response.cookies["ff_pending_device_token"].value, str(device.device_token))

    def test_pending_view_while_pending(self):
        device = AuthorizedDevice.objects.create(
            device_name="Dell Latitude 7420",
            requested_by_name="Staff Member",
            status="PENDING",
        )
        self.client.cookies["ff_pending_device_token"] = str(device.device_token)

        response = self.client.get(reverse("device_pending"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Device Authorization Pending")
        self.assertContains(response, "Dell Latitude 7420")

    def test_pending_view_when_approved(self):
        device = AuthorizedDevice.objects.create(
            device_name="Dell Latitude 7420",
            requested_by_name="Staff Member",
            status="APPROVED",
        )
        self.client.cookies["ff_pending_device_token"] = str(device.device_token)

        response = self.client.get(reverse("device_pending"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/login/")

        # Permanent cookie set and pending cookie deleted
        self.assertIn("ff_device_token", response.cookies)
        self.assertEqual(response.cookies["ff_device_token"].value, str(device.device_token))

    def test_status_check_ajax_endpoint(self):
        device = AuthorizedDevice.objects.create(
            device_name="Asus ExpertBook",
            requested_by_name="Staff Member",
            status="PENDING",
        )
        self.client.cookies["ff_pending_device_token"] = str(device.device_token)

        # 1. Status is pending
        response = self.client.get(reverse("device_status_check"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "PENDING")

        # 2. Admin approves device
        device.status = "APPROVED"
        device.approved_at = timezone.now()
        device.save()

        # 3. Next status check receives APPROVED and gets permanent cookie
        response = self.client.get(reverse("device_status_check"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "APPROVED")
        self.assertEqual(data["redirect_url"], "/login/")
        self.assertIn("ff_device_token", response.cookies)
        self.assertEqual(response.cookies["ff_device_token"].value, str(device.device_token))

    def test_unauthorized_view(self):
        response = self.client.get(reverse("device_unauthorized"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Access Restricted")


@override_settings(
    DEVICE_RESTRICTION_TESTING=True,
    DEVICE_RESTRICTION_MODE="ENFORCE",
)
class DeviceGatekeeperMiddlewareTest(TestCase):
    """Integration tests for DeviceGatekeeperMiddleware."""

    def setUp(self):
        self.client = Client()
        self.device = AuthorizedDevice.objects.create(
            device_name="Company Laptop #1",
            requested_by_name="John Doe",
            status="APPROVED",
        )
        self.super_admin = EmployeeUser.objects.create_user(
            email="superadmin@flow-force.com",
            full_name="Super Administrator",
            password="password123",
            role="SUPER_ADMIN",
            status="APPROVED",
            is_staff=True,
            is_superuser=True,
        )

    def test_unauthorized_request_redirects_to_register(self):
        # Accessing protected portal route /login/ or / without cookie
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/device/register/")

    def test_pending_cookie_redirects_to_pending(self):
        pending_dev = AuthorizedDevice.objects.create(
            device_name="Pending Laptop",
            requested_by_name="Jane",
            status="PENDING",
        )
        self.client.cookies["ff_pending_device_token"] = str(pending_dev.device_token)
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/device/pending/")

    def test_approved_device_cookie_allowed(self):
        self.client.cookies["ff_device_token"] = str(self.device.device_token)
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)

    def test_revoked_device_cookie_redirects_to_unauthorized(self):
        self.device.status = "REVOKED"
        self.device.save()
        self.client.cookies["ff_device_token"] = str(self.device.device_token)

        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/device/unauthorized/")

    def test_exempt_paths_allowed_without_cookie(self):
        # /device/ paths must be exempt
        resp1 = self.client.get("/device/register/")
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.get("/device/unauthorized/")
        self.assertEqual(resp2.status_code, 200)

        # /admin/login/ must be exempt for admin fallback
        resp3 = self.client.get("/admin/login/")
        self.assertEqual(resp3.status_code, 200)

    def test_superadmin_bypass(self):
        self.client.force_login(self.super_admin)
        # Even without device cookie, super admin passes
        response = self.client.get("/login/")
        # login view redirects authenticated user to / (or returns 302/200), not /device/register/
        self.assertNotEqual(response.url if response.status_code == 302 else None, "/device/register/")

    @override_settings(DEVICE_BYPASS_TOKEN="test_secret_bypass_token")
    def test_bypass_token_header(self):
        response = self.client.get("/login/", headers={"X-Device-Bypass-Token": "test_secret_bypass_token"})
        self.assertEqual(response.status_code, 200)

    @override_settings(DEVICE_RESTRICTION_MODE="LOG_ONLY")
    def test_soft_mode_allows_all(self):
        # In soft mode (LOG_ONLY), unauthenticated requests without cookie are allowed through
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
