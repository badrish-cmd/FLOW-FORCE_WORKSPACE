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
        # Check redirect
        self.assertEqual(response.status_code, 302)
        # Verify that the session has the expected 10-year age
        session = self.client.session
        # Check that session expiry age is equal to SESSION_COOKIE_AGE
        self.assertEqual(session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        # Check that it doesn't expire at browser close
        self.assertFalse(session.get_expire_at_browser_close())

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
