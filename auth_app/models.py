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
    