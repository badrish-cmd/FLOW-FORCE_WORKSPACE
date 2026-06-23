from django.core.management.base import BaseCommand
from django.utils import timezone
from task_tracker.models import EmailLog
from task_tracker.services import send_due_task_alerts, send_overdue_escalations

class Command(BaseCommand):
    help = "Run the workspace automation engine: send alerts, escalations, and retry failed emails."

    def handle(self, *args, **options):
        self.stdout.write("Starting Flow-Force Automation Engine...")
        
        # 1. Run due alerts
        self.stdout.write("Running daily due task reminders...")
        sent_alerts = send_due_task_alerts()
        self.stdout.write(self.style.SUCCESS(f"Due alerts sent: {sent_alerts}"))
        
        # 2. Run overdue task escalations
        self.stdout.write("Running overdue task escalations...")
        sent_escalations = send_overdue_escalations()
        self.stdout.write(self.style.SUCCESS(f"Escalation emails sent: {sent_escalations}"))
        
        # 3. Retry mechanism for failed emails
        self.stdout.write("Retrying failed emails (attempts < 5)...")
        failed_emails = EmailLog.objects.filter(status="FAILED", retry_count__lt=5)
        retry_success_count = 0
        retry_failed_count = 0
        
        from django.core.mail import send_mail
        from django.conf import settings
        
        for email_log in failed_emails:
            email_log.retry_count += 1
            try:
                send_mail(
                    subject=email_log.subject,
                    message=email_log.body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[email_log.recipient],
                    fail_silently=False
                )
                email_log.status = "SENT"
                email_log.error_message = None
                retry_success_count += 1
            except Exception as e:
                email_log.status = "FAILED"
                email_log.error_message = str(e)
                retry_failed_count += 1
            email_log.save()
            
        self.stdout.write(self.style.SUCCESS(
            f"Retry run complete. Succeeded: {retry_success_count}, Failed: {retry_failed_count}"
        ))
