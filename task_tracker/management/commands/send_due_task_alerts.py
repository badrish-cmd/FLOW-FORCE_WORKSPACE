from django.core.management.base import BaseCommand

from task_tracker.services import send_due_task_alerts


class Command(BaseCommand):
    help = "Send due-date reminder emails for tasks that are due today."

    def handle(self, *args, **options):
        sent_count = send_due_task_alerts()
        self.stdout.write(self.style.SUCCESS(f"Sent {sent_count} due-task reminder email(s)."))
