if __name__ == "__main__":
    from django.core.mail import send_mail

    send_mail(
        subject='Flow-Force SMTP Test',
        message='SMTP is configured correctly.',
        from_email='operations.flowforce@gmail.com',
        recipient_list=['operations@flow-force.com'],
        fail_silently=False,
    )

    print("EMAIL SENT")