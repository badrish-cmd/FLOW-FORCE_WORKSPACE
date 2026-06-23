from django.core.management.base import BaseCommand

from task_tracker.services import send_overdue_escalations


class Command(BaseCommand):
    help = "Send overdue task escalations according to configured thresholds"

    def handle(self, *args, **options):
        sent = send_overdue_escalations()
        self.stdout.write(self.style.SUCCESS(f"Escalation notifications sent: {sent}"))
