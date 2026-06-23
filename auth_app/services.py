import random

from django.core.mail import send_mail
from django.conf import settings

from .models import PasswordResetOTP


def generate_otp():
    return str(random.randint(100000, 999999))


def send_otp_email(user, otp):

    subject = "Flow-Force Workspace Password Reset OTP"

    message = f"""
Hello {user.full_name},

Your password reset OTP is:

{otp}

This OTP is valid for 10 minutes.

If you did not request this reset, please ignore this email.

Flow-Force Workspace
"""

    from task_tracker.services import log_and_send_email
    log_and_send_email(
        subject=subject,
        message=message,
        recipient_list=[user.email],
    )