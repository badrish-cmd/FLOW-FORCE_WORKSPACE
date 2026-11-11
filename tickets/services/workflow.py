"""
Business rules, validation, permissions, and notifications for Tickets & Projects.
"""
import logging
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from ..models import Ticket, TicketEvent, TicketFile

logger = logging.getLogger(__name__)
User = get_user_model()


def _site_url():
    return getattr(settings, "SITE_URL", "https://flowforceworkspace.cloud").rstrip("/")


def is_ticket_manager(user):
    """Check if user has administrative rights over tickets."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or getattr(user, "role", "") == "SUPER_ADMIN":
        return True
    if user.groups.filter(name="Ticket Managers").exists():
        return True
    return False


def can_view(user, ticket=None):
    """All active employees can view tickets."""
    return bool(user and user.is_authenticated and user.is_active)


def can_create_ticket(user):
    """All active employees can create tickets."""
    return bool(user and user.is_authenticated and user.is_active)


def can_upload_files(user, ticket=None):
    """All active employees can upload files."""
    return bool(user and user.is_authenticated and user.is_active)


def can_add_note(user, ticket=None):
    """All active employees can add notes."""
    return bool(user and user.is_authenticated and user.is_active)


def can_edit_ticket(user, ticket):
    """Only assignee, creator, super admin, or Ticket Managers can edit header/details."""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if is_ticket_manager(user):
        return True
    return bool(ticket and (ticket.assigned_to_id == user.id or ticket.created_by_id == user.id))


def can_change_status(user, ticket):
    """Only assignee, creator, super admin, or Ticket Managers can advance status."""
    return can_edit_ticket(user, ticket)


def can_delete_ticket(user, ticket=None):
    """Only SUPER_ADMIN or members of 'Ticket Managers' can soft-delete tickets."""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    return is_ticket_manager(user)


def can_override_status_rules(user):
    """Only superadmins can override missing file requirements."""
    if not user or not user.is_authenticated:
        return False
    return user.is_superuser or getattr(user, "role", "") == "SUPER_ADMIN"


def validate_status_transition(ticket, new_status, user, override_reason=""):
    """
    Validate whether the ticket can transition to new_status based on uploaded files.
    Returns (is_valid, is_override, error_message).
    """
    if new_status == ticket.status:
        return True, False, ""

    errors = []
    active_files = ticket.files.filter(is_hidden=False)

    if ticket.type == Ticket.TYPE_PROJECT:
        # PROJECT Job Rules
        if new_status == Ticket.STATUS_HANDOVER:
            has_report = active_files.filter(category=TicketFile.CATEGORY_SITE_REPORT).exists()
            has_photo = active_files.filter(category=TicketFile.CATEGORY_PHOTO).exists()
            if not has_report:
                errors.append("At least one Site Report is required before moving to Handover.")
            if not has_photo:
                errors.append("At least one Photo is required before moving to Handover.")

        elif new_status == Ticket.STATUS_CLOSED:
            has_warranty = active_files.filter(category=TicketFile.CATEGORY_WARRANTY).exists()
            if not has_warranty:
                errors.append("At least one Warranty document is required before closing a Project Job.")

    elif ticket.type == Ticket.TYPE_SERVICE:
        # SERVICE Ticket Rules
        if new_status in (Ticket.STATUS_COMPLETED, Ticket.STATUS_CLOSED):
            has_report = active_files.filter(category=TicketFile.CATEGORY_SITE_REPORT).exists()
            if not has_report:
                errors.append("At least one Site Report is required before completing or closing a Service Ticket.")

            if ticket.parts_purchased:
                has_receipt = active_files.filter(category=TicketFile.CATEGORY_RECEIPT).exists()
                if not has_receipt:
                    errors.append("Parts were purchased for this ticket: at least one Receipt is required.")

    if not errors:
        return True, False, ""

    # Check for Super Admin override
    if can_override_status_rules(user):
        clean_reason = (override_reason or "").strip()
        if clean_reason:
            return True, True, ""
        else:
            return False, False, f"{'; '.join(errors)} (As a Super Admin, you may override by providing an override reason)."

    return False, False, "; ".join(errors)


def change_ticket_status(ticket, new_status, user, note="", override_reason=""):
    """
    Advance or update ticket status with business validation, event logging, and notification.
    """
    if not can_change_status(user, ticket):
        raise PermissionDenied("You do not have permission to change this ticket's status.")

    is_valid, is_override, err_msg = validate_status_transition(ticket, new_status, user, override_reason)
    if not is_valid:
        raise ValidationError(err_msg)

    old_status = ticket.status
    ticket.status = new_status
    ticket.status_changed_at = timezone.now()
    ticket.save(update_fields=["status", "status_changed_at", "updated_at"])

    event_action = "STATUS_OVERRIDE" if is_override else "STATUS_CHANGE"
    event_note_parts = []
    if note.strip():
        event_note_parts.append(note.strip())
    if is_override:
        event_note_parts.append(f"[OVERRIDE REASON: {override_reason.strip()}]")

    full_note = "\n\n".join(event_note_parts)

    event = TicketEvent.objects.create(
        ticket=ticket,
        user=user,
        action=event_action,
        from_status=old_status,
        to_status=new_status,
        note=full_note
    )

    actor_name = getattr(user, "full_name", None) or getattr(user, "email", "") or "System"
    notify_ticket_event(
        ticket=ticket,
        title=f"Ticket {ticket.number} status updated to {ticket.status_label}",
        message=f"Status changed from {old_status} to {ticket.status_label} by {actor_name}.\n{full_note}",
        kind="STATUS_UPDATE",
        exclude_user=user
    )

    return event


def soft_delete_ticket(ticket, user, reason=""):
    """
    Soft-delete a ticket. Never deletes files from Synology NAS.
    """
    if not can_delete_ticket(user, ticket):
        raise PermissionDenied("Only Super Admins or Ticket Managers can delete tickets.")

    ticket.is_deleted = True
    ticket.save(update_fields=["is_deleted", "updated_at"])

    TicketEvent.objects.create(
        ticket=ticket,
        user=user,
        action="DELETED",
        note=f"Ticket soft-deleted. Reason: {reason or 'Not specified'}"
    )

    actor_name = getattr(user, "full_name", None) or getattr(user, "email", "") or "System"
    notify_ticket_event(
        ticket=ticket,
        title=f"Ticket {ticket.number} deleted",
        message=f"Ticket {ticket.number} was deleted by {actor_name}.",
        kind="DELETED",
        exclude_user=user
    )


def notify_ticket_event(ticket, title, message, kind="INFO", exclude_user=None):
    """
    Create in-app notifications and queue emails to relevant stakeholders.
    """
    recipients = set()
    if ticket.assigned_to:
        recipients.add(ticket.assigned_to)
    if ticket.created_by:
        recipients.add(ticket.created_by)

    # Also notify superusers / managers on critical events
    if kind in ("STATUS_UPDATE", "DELETED"):
        managers = User.objects.filter(is_active=True).filter(
            is_superuser=True
        ) | User.objects.filter(is_active=True, groups__name="Ticket Managers")
        for m in managers[:10]:
            recipients.add(m)

    if exclude_user:
        recipients.discard(exclude_user)

    link = reverse("tickets:ticket_detail", args=[ticket.id]) if ticket.id else "/tickets/"
    full_url = _site_url() + link

    for u in recipients:
        try:
            from tasks.models import Notification
            Notification.objects.create(
                user=u,
                title=title,
                description=message,
                type=kind,
                link=link
            )
        except Exception as e:
            logger.warning("Could not create in-app notification for %s: %s", u, e)

        # Queue email if user has email and email service exists
        if u.email:
            try:
                from tasks.models import EmailLog
                from tasks.tasks import send_email_log_task

                recipient_name = getattr(u, "full_name", None) or getattr(u, "email", "") or "User"
                html = render_to_string("tickets/email_notice.html", {
                    "recipient_name": recipient_name,
                    "title": title,
                    "message": message,
                    "ticket": ticket,
                    "url": full_url,
                })
                elog = EmailLog.objects.create(
                    recipient=u.email,
                    subject=f"[Flow-Force Tickets] {title}",
                    message=html,
                    email_type="TICKET_MAIL",
                )
                try:
                    send_email_log_task.delay(elog.id)
                except Exception:
                    # Celery / Redis might be offline locally
                    pass
            except Exception as e:
                logger.debug("Could not queue email for %s: %s", u.email, e)
