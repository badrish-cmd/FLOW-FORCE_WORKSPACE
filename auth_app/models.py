from django.db import models
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin

from .managers import EmployeeUserManager


class EmployeeUser(
    AbstractBaseUser,
    PermissionsMixin
):

    ROLE_CHOICES = [

        ("SUPER_ADMIN", "Super Admin"),

        ("ADMIN", "Admin"),

        (
            "DEPARTMENT_ADMIN",
            "Department Admin"
        ),

        ("EMPLOYEE", "Employee"),

    ]
    STATUS_CHOICES = [

    ("PENDING", "Pending"),

    ("APPROVED", "Approved"),

    ("REJECTED", "Rejected"),

]

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING"
    )


    full_name = models.CharField(
        max_length=150
    )

    email = models.EmailField(
        unique=True
    )

    department = models.ForeignKey(
        'employee_management.Department',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees'
    )

    role = models.CharField(
        max_length=30,
        choices=ROLE_CHOICES,
        default="EMPLOYEE"
    )

    is_active = models.BooleanField(
        default=True
    )

    is_staff = models.BooleanField(
        default=False
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    objects = EmployeeUserManager()

    USERNAME_FIELD = "email"

    REQUIRED_FIELDS = [
        "full_name"
    ]

    def __str__(self):
        return (
            f"{self.full_name}"
            f" ({self.email})"
        )
from django.utils import timezone
from datetime import timedelta


class PasswordResetOTP(models.Model):

    user = models.ForeignKey(
        EmployeeUser,
        on_delete=models.CASCADE
    )

    otp_code = models.CharField(
        max_length=6
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True
    )

    is_used = models.BooleanField(
        default=False
    )

    def save(self, *args, **kwargs):

        if not self.expires_at:
            self.expires_at = (
                timezone.now() +
                timedelta(minutes=10)
            )

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.user.email}"
            f" - {self.otp_code}"
        )


import uuid
from django.conf import settings


class AuthorizedDevice(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending Approval"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("REVOKED", "Revoked / Blocked"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    device_name = models.CharField(max_length=150, help_text="e.g. HP ProBook - Accounting #03")
    asset_tag = models.CharField(max_length=100, blank=True, null=True, help_text="Company asset tag / Serial")

    # Ownership & Assignment
    requested_by_name = models.CharField(max_length=150, help_text="Name entered by employee at registration")
    employee_email = models.EmailField(blank=True, null=True, help_text="Employee company email")
    department = models.CharField(max_length=100, blank=True, default="", help_text="Department name")
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authorized_devices",
        help_text="Link to official employee profile"
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING", db_index=True)

    # Audit & Diagnostics
    registration_ip = models.GenericIPAddressField(blank=True, null=True)
    user_agent_summary = models.TextField(blank=True, help_text="Browser/OS string detected at registration")
    last_seen_ip = models.GenericIPAddressField(blank=True, null=True)
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_devices"
    )

    class Meta:
        verbose_name = "Authorized Device"
        verbose_name_plural = "Authorized Devices"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.device_name} ({self.requested_by_name}) - [{self.status}]"

    @property
    def is_approved(self):
        return self.status == "APPROVED"

    