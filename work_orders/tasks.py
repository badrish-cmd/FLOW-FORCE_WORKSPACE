import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)
MAX_ATTACH_BYTES = 15 * 1024 * 1024


@shared_task
def issue_work_order(wo_id):
    """After GM approval: make the PDF, then email the assigned employees with the PDF, images and files."""
    from tasks.models import EmailLog
    from . import services
    from .models import WorkOrder
    wo = WorkOrder.objects.select_related("team", "created_by", "approved_by").filter(pk=wo_id).first()
    if not wo or not wo.number:
        return
    pdf = services.generate_pdf(wo)
    url = services._site() + reverse("work_orders:detail", args=[wo.id])
    files, linked, total = [], [], len(pdf)
    for a in wo.attachments.filter(by_assignee=False):
        try:
            size = a.file.size
            if total + size > MAX_ATTACH_BYTES:
                linked.append(a.filename)
                continue
            with a.file.open("rb") as fh:
                files.append((a.filename, fh.read()))
            total += size
        except Exception as e:
            logger.warning("attachment %s: %s", a.file.name, e)
    title = f"Work Order {wo.number}"
    message = (f"{wo.approved_by.full_name if wo.approved_by else 'GM'} approved this work order for you "
               f"({wo.pid_label}, {wo.type_label + ' - all departments' if wo.is_project else wo.team.name}). "
               f"The work order PDF and the files are attached."
               + (" Tick your tasks in the workspace as you finish them." if wo.is_project else ""))
    assignees = list(wo.assignees.filter(is_active=True))
    services.notify(assignees, f"New work order: {wo.number}", message, wo)
    for u in assignees:
        if not u.email:
            continue
        html = render_to_string("work_orders/email.html", {"name": u.full_name or u.email, "title": title,
                                                           "message": message, "wo": wo, "url": url,
                                                           "files": [n for n, _ in files], "linked": linked})
        subject = (f"[Flow-Force] Work Order {wo.number} - Internal - {wo.reference}" if wo.is_internal
                   else f"[Flow-Force] Work Order {wo.number} - PID {wo.pid} - {wo.customer_name}")[:255]
        log = EmailLog.objects.create(recipient_email=u.email, subject=subject, body=html,
                                      email_type="WORK_ORDER_MAIL", status="PENDING", max_retries=1)
        try:
            msg = EmailMultiAlternatives(subject, f"{title}\n\n{message}\n\nOpen: {url}", settings.DEFAULT_FROM_EMAIL, [u.email])
            msg.attach_alternative(html, "text/html")
            msg.attach(f"{wo.number}.pdf", pdf, "application/pdf")
            for name, data in files:
                msg.attach(name, data)
            msg.send(fail_silently=False)
            log.status, log.sent_at = "SENT", timezone.now()
        except Exception as e:
            logger.exception("work order mail to %s failed", u.email)
            log.status, log.error_message = "FAILED", str(e)[:1000]
        log.save()
    services.event(wo, "issued", f"PDF made and emailed to {', '.join(u.full_name or u.email for u in assignees) or 'nobody'}", None)


@shared_task
def send_overdue_alerts():
    from .project_services import overdue_alerts
    return overdue_alerts()
