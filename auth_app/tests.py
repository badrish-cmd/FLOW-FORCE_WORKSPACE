from django.test import TestCase
from django.urls import reverse
from django.conf import settings
from .models import EmployeeUser

class LoginSessionTests(TestCase):
    def setUp(self):
        # Create an approved employee user for testing
        self.user = EmployeeUser.objects.create_user(
            email="test.user@flow-force.com",
            password="testpassword123",
            full_name="Test User",
            role="EMPLOYEE",
            status="APPROVED"
        )
        self.login_url = reverse("login")
        self.logout_url = reverse("logout")

    def test_login_sets_forever_session(self):
        # Post to the login url
        response = self.client.post(self.login_url, {
            "email": "test.user@flow-force.com",
            "password": "testpassword123"
        })
        # Check redirect to employee dashboard
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("employee_dashboard"))
        # Verify that the session has the expected 10-year age
        session = self.client.session
        # Check that session expiry age is equal to SESSION_COOKIE_AGE
        self.assertEqual(session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        # Check that it doesn't expire at browser close
        self.assertFalse(session.get_expire_at_browser_close())

    def test_login_invalid_password_rejected(self):
        response = self.client.post(self.login_url, {
            "email": "test.user@flow-force.com",
            "password": "wrongpassword"
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid email or password.")
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_nonexistent_user_rejected(self):
        response = self.client.post(self.login_url, {
            "email": "nonexistent@flow-force.com",
            "password": "testpassword123"
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid email or password.")
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_pending_user_rejected(self):
        pending_user = EmployeeUser.objects.create_user(
            email="pending@flow-force.com",
            password="testpassword123",
            full_name="Pending User",
            status="PENDING"
        )
        response = self.client.post(self.login_url, {
            "email": "pending@flow-force.com",
            "password": "testpassword123"
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your account is awaiting approval.")
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_rejected_user_rejected(self):
        rejected_user = EmployeeUser.objects.create_user(
            email="rejected@flow-force.com",
            password="testpassword123",
            full_name="Rejected User",
            status="REJECTED"
        )
        response = self.client.post(self.login_url, {
            "email": "rejected@flow-force.com",
            "password": "testpassword123"
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your account has been rejected.")
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_disabled_user_rejected(self):
        disabled_user = EmployeeUser.objects.create_user(
            email="disabled@flow-force.com",
            password="testpassword123",
            full_name="Disabled User",
            status="APPROVED",
            is_active=False
        )
        response = self.client.post(self.login_url, {
            "email": "disabled@flow-force.com",
            "password": "testpassword123"
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Account disabled.")
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_create_superuser_defaults_to_approved(self):
        su = EmployeeUser.objects.create_superuser(
            email="superadmin@flow-force.com",
            password="superpassword123",
            full_name="Super Admin"
        )
        self.assertEqual(su.status, "APPROVED")
        self.assertTrue(su.is_superuser)
        self.assertTrue(su.is_staff)
        self.assertTrue(su.is_active)
        self.assertEqual(su.role, "SUPER_ADMIN")

    def test_logout_clears_session(self):
        # Log in first
        self.client.login(username="test.user@flow-force.com", password="testpassword123")
        # Ensure session exists
        self.assertTrue(self.client.session.keys())
        
        # Log out
        response = self.client.get(self.logout_url)
        self.assertEqual(response.status_code, 302)
        # Session should be empty or deleted
        self.assertNotIn('_auth_user_id', self.client.session)

class RegistrationTests(TestCase):
    def test_registration_allowed_domains(self):
        # Test registering flow-force.com domain
        response1 = self.client.post(reverse("register"), {
            "email": "new.user@flow-force.com",
            "password": "testpassword123",
            "confirm_password": "testpassword123",
            "full_name": "New User 1"
        })
        self.assertEqual(response1.status_code, 302)
        self.assertTrue(EmployeeUser.objects.filter(email="new.user@flow-force.com").exists())

        # Test registering flowforceengineering.com domain
        response2 = self.client.post(reverse("register"), {
            "email": "new.eng@flowforceengineering.com",
            "password": "testpassword123",
            "confirm_password": "testpassword123",
            "full_name": "New User 2"
        })
        self.assertEqual(response2.status_code, 302)
        self.assertTrue(EmployeeUser.objects.filter(email="new.eng@flowforceengineering.com").exists())

        # Test registering unallowed domain (e.g. gmail.com)
        response3 = self.client.post(reverse("register"), {
            "email": "hacker@gmail.com",
            "password": "testpassword123",
            "confirm_password": "testpassword123",
            "full_name": "Hacker"
        })
        # It should redirect back to register with error
        self.assertEqual(response3.status_code, 302)
        self.assertFalse(EmployeeUser.objects.filter(email="hacker@gmail.com").exists())
