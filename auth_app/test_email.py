if __name__ == "__main__":
    from django.conf import settings
    from django.core.mail import send_mail

    recipient = settings.EMAIL_HOST_USER or 'noreply@flowforce.local'
    send_mail(
        subject='Flow-Force SMTP Test',
        message='SMTP is configured correctly.',
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=False,
    )

    print("EMAIL SENT")