from celery import shared_task

from .services import send_due_task_alerts, send_overdue_escalations


@shared_task(name="task_tracker.send_due_task_alerts")
def send_due_task_alerts_task():
    return send_due_task_alerts()


@shared_task(name="task_tracker.send_overdue_escalations")
def send_overdue_escalations_task():
    return send_overdue_escalations()


@shared_task(name="task_tracker.send_async_email")
def send_async_email_task(subject, message, recipient_list, from_email=None):
    from .services import log_and_send_email_sync
    return log_and_send_email_sync(subject, message, recipient_list, from_email)

