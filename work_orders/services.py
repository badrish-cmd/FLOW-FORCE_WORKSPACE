"""Access, numbering and workflow for Work Orders."""
import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import (STAGE_CHECKLISTS, Attachment, ChecklistItem, Event, NumberCounter, Team, WorkOrder,
                     WorkOrderAccess, WorkOrderAccessChange, WorkOrderLine, Note)

logger = logging.getLogger(__name__)
User = get_user_model()

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")
LEVEL_ADMIN = "ADMIN"
ALLOWED_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".xlsx", ".xls", ".docx", ".doc",
               ".dwg", ".dxf", ".zip", ".txt", ".csv")
MAX_FILE_MB = 25


class WorkOrderError(Exception):
    pass


# ---------------------------------------------------------------- access

def is_admin(user):
    return bool(user and user.is_authenticated and
                (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES))


def level(user):
    if not user or not user.is_authenticated:
        return None
    if not hasattr(user, "_wo_level"):
        if is_admin(user):
            user._wo_level = LEVEL_ADMIN
        else:
            row = WorkOrderAccess.objects.filter(user=user).only("level").first()
            user._wo_level = row.level if row else None
    return user._wo_level


def approver_roles():
    return tuple(getattr(settings, "WORK_ORDER_APPROVER_ROLES", ("SUPER_ADMIN",)))


def can_approve(user):
    return bool(user and user.is_authenticated and
                (user.is_superuser or getattr(user, "role", None) in approver_roles()
                 or user.groups.filter(name="Work Order Approvers").exists()))


def can_create(user):
    return level(user) in (LEVEL_ADMIN, WorkOrderAccess.LEVEL_CREATOR)


def sees_all(user):
    return level(user) is not None or can_approve(user)


def is_assignee(user, wo):
    return wo.assignees.filter(pk=user.pk).exists()


def can_see(user, wo):
    hidden = WorkOrder.EDITABLE + (WorkOrder.STATUS_IN_REVIEW,)
    return sees_all(user) or (wo.status not in hidden and is_assignee(user, wo))


def can_use(user):
    """Sidebar: admins, access rows, approvers, or anyone with a work order assigned."""
    if not user or not user.is_authenticated:
        return False
    return sees_all(user) or user.assigned_work_orders.exclude(status__in=WorkOrder.EDITABLE + (WorkOrder.STATUS_IN_REVIEW,)).exists()


def can_edit(user, wo):
    return wo.is_editable and (is_admin(user) or (can_create(user) and wo.created_by_id == user.id))


def can_delete(user, wo):
    return can_edit(user, wo)


def can_work(user, wo):
    """Tick the checklist, add notes/photos, start / mark done."""
    return wo.status in WorkOrder.ACTIVE and (is_assignee(user, wo) or is_admin(user) or wo.created_by_id == user.id)


def can_close(user, wo):
    return wo.status in (WorkOrder.STATUS_DONE,) + WorkOrder.ACTIVE and (
        can_approve(user) or is_admin(user) or wo.created_by_id == user.id)


def visible_qs(user):
    qs = WorkOrder.objects.select_related("team", "created_by", "approved_by").prefetch_related("assignees")
    if sees_all(user):
        return qs
    return qs.filter(assignees=user).exclude(status__in=WorkOrder.EDITABLE + (WorkOrder.STATUS_IN_REVIEW,)).distinct()


@transaction.atomic
def set_access(target, new_level, by):
    new_level = new_level or ""
    if new_level not in ("", WorkOrderAccess.LEVEL_VIEWER, WorkOrderAccess.LEVEL_CREATOR):
        raise WorkOrderError("Unknown access level.")
    if is_admin(target):
        raise WorkOrderError(f"{target.full_name or target.email} is an Admin and always has full access.")
    row = WorkOrderAccess.objects.filter(user=target).first()
    old = row.level if row else ""
    if old == new_level:
        return False
    if new_level:
        WorkOrderAccess.objects.update_or_create(user=target, defaults={"level": new_level, "granted_by": by})
    elif row:
        row.delete()
    WorkOrderAccessChange.objects.create(user=target, changed_by=by, old_level=old, new_level=new_level)
    return True


# ---------------------------------------------------------------- helpers

def event(wo, action, message, user):
    Event.objects.create(work_order=wo, label=wo.display_number if wo else "", user=user, action=action, message=message)


def _site():
    return getattr(settings, "SITE_URL", "https://flowforceworkspace.cloud").rstrip("/")


def employees():
    return User.objects.filter(is_active=True).order_by("full_name", "email")


def validate_file(f):
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise WorkOrderError(f"{f.name}: file type not allowed.")
    if f.size > MAX_FILE_MB * 1024 * 1024:
        raise WorkOrderError(f"{f.name} is larger than {MAX_FILE_MB} MB.")


def notify(users, title, message, wo):
    from tasks.models import Notification
    link = reverse("work_orders:detail", args=[wo.id])
    for u in users:
        if u and u.is_active:
            Notification.objects.create(user=u, title=title, description=message, type="SYSTEM", link=link)


def _simple_mail(users, subject, title, message, wo):
    from django.template.loader import render_to_string
    from tasks.models import EmailLog
    from tasks.tasks import send_email_log_task
    url = _site() + reverse("work_orders:detail", args=[wo.id])
    for u in users:
        if not (u and u.is_active and u.email):
            continue
        html = render_to_string("work_orders/email.html", {"name": u.full_name or u.email, "title": title,
                                                           "message": message, "wo": wo, "url": url, "files": []})
        log = EmailLog.objects.create(recipient_email=u.email, subject=f"[Flow-Force] {subject}", body=html,
                                      email_type="WORK_ORDER_MAIL", status="PENDING", max_retries=3)
        transaction.on_commit(lambda i=log.id: send_email_log_task.delay(i))


def approvers():
    roles = approver_roles()
    from django.db.models import Q
    return list(User.objects.filter(is_active=True).filter(Q(role__in=roles) | Q(groups__name="Work Order Approvers")).distinct())


# ---------------------------------------------------------------- numbering

def next_number(wo, year=None):
    """WO-26-1932-ENG-01: year of approval, PID, team code, count per year/PID/team."""
    year = year or timezone.localdate().year
    yy = year % 100
    pid = "".join(ch for ch in wo.pid if ch.isalnum()) or "0"
    counter, _ = NumberCounter.objects.select_for_update().get_or_create(year=year, pid=pid, team_code=wo.team.code)
    while True:
        counter.last += 1
        base = f"WO-{yy:02d}-{pid}-{wo.team.code}-{counter.last:02d}"
        if not WorkOrder.objects.filter(base_number=base).exists():
            break
    counter.save(update_fields=["last"])
    return base


# ---------------------------------------------------------------- create / edit

def _apply_form(wo, data):
    for f in ("customer_name", "invoice_address", "delivery_address", "customer_po", "quotation_no", "sales_order_no",
              "job_no", "delivery_method", "title", "instructions", "notes", "terms"):
        if f in data:
            setattr(wo, f, (data.get(f) or "").strip())
    for f in ("order_date", "required_by"):
        if f in data:
            setattr(wo, f, data.get(f) or None)
    if "ppn_percent" in data and data.get("ppn_percent") is not None:
        wo.ppn_percent = data["ppn_percent"]


def _set_lines(wo, lines):
    wo.lines.all().delete()
    for i, ln in enumerate(lines):
        desc = (ln.get("description") or "").strip()
        if not desc:
            continue
        WorkOrderLine.objects.create(work_order=wo, position=i, description=desc, qty=ln.get("qty"),
                                     unit=(ln.get("unit") or "").strip()[:30], rate=ln.get("rate"),
                                     discount=ln.get("discount"), source_row_id=ln.get("row_id"))


def _set_stages(wo, stages):
    stages = sorted({int(s) for s in stages if str(s).isdigit() and 1 <= int(s) <= 10})
    old = set(wo.stages or [])
    wo.stages = stages
    for s in set(stages) - old:
        for i, text in enumerate(STAGE_CHECKLISTS.get(s, [])):
            ChecklistItem.objects.create(work_order=wo, stage=s, text=text, position=i)
    wo.checklist.exclude(stage__in=stages).delete()


def _add_files(wo, files, user, captions=None, in_pdf=True, by_assignee=False):
    for i, f in enumerate(files or []):
        validate_file(f)
        a = Attachment(work_order=wo, uploaded_by=user, by_assignee=by_assignee, include_in_pdf=in_pdf,
                       original_name=os.path.basename(f.name)[:255],
                       caption=((captions or [])[i] if captions and i < len(captions) else "")[:255])
        a.file.save(os.path.basename(f.name), f, save=True)


@transaction.atomic
def save_draft(wo, data, user, *, lines, stages, sections, assignee_ids, files=None):
    creating = wo.pk is None
    if not creating and not can_edit(user, wo):
        raise WorkOrderError("This work order can't be edited now.")
    if creating and not can_create(user):
        raise WorkOrderError("You don't have permission to create work orders.")
    for f in files or []:
        validate_file(f)
    from .models import SECTION_KEYS
    _apply_form(wo, data)
    wo.sections = [s for s in SECTION_KEYS if s in set(sections or [])]
    if creating:
        wo.created_by = user
    wo.save()
    _set_stages(wo, stages or [])
    wo.save(update_fields=["stages"])
    _set_lines(wo, lines)
    ids = [int(i) for i in assignee_ids if str(i).isdigit()]
    wo.assignees.set(User.objects.filter(pk__in=ids, is_active=True))
    _add_files(wo, files, user)
    event(wo, "created" if creating else "edited", "Draft created" if creating else "Draft edited", user)
    return wo


@transaction.atomic
def delete_draft(wo, user):
    if not can_delete(user, wo):
        raise WorkOrderError("Only drafts (or sent-back work orders) can be deleted.")
    label = wo.display_number
    files = [a.file for a in wo.attachments.all()]
    Event.objects.create(work_order=None, label=label, user=user, action="deleted",
                         message=f"Draft {label} for PID {wo.pid} ({wo.team.code}) deleted")
    wo.delete()

    def _rm():
        for f in files:
            try:
                f.storage.delete(f.name)
            except Exception as e:
                logger.warning("delete %s: %s", f.name, e)
    transaction.on_commit(_rm)


# ---------------------------------------------------------------- workflow

def _check_ready(wo):
    problems = []
    if not wo.assignees.exists():
        problems.append("Pick at least one employee to send the work order to.")
    if not wo.lines.exists() and not wo.instructions.strip():
        problems.append("Add item lines or instructions.")
    return problems


@transaction.atomic
def submit(wo, user):
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if not can_edit(user, wo):
        raise WorkOrderError("This work order can't be submitted now.")
    problems = _check_ready(wo)
    if problems:
        raise WorkOrderError(" ".join(problems))
    wo.status = WorkOrder.STATUS_IN_REVIEW
    wo.submitted_at = timezone.now()
    wo.save(update_fields=["status", "submitted_at", "updated_at"])
    event(wo, "submitted", "Sent to GM for approval", user)
    msg = f"{user.full_name or user.email} sent a work order for {wo.pid_label} ({wo.team.name}) for your approval."
    notify(approvers(), "Work order waiting for approval", msg, wo)
    _simple_mail(approvers(), f"Work order for approval: {wo.pid_label} - {wo.team.name}", "Work order waiting for approval", msg, wo)
    return wo


@transaction.atomic
def send_back(wo, user, comment):
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if wo.status != WorkOrder.STATUS_IN_REVIEW or not can_approve(user):
        raise WorkOrderError("Only the GM can send back a work order that is waiting for approval.")
    comment = (comment or "").strip()
    if not comment:
        raise WorkOrderError("Please write why it is sent back.")
    wo.status = WorkOrder.STATUS_SENT_BACK
    wo.gm_comment = comment
    wo.save(update_fields=["status", "gm_comment", "updated_at"])
    event(wo, "sent_back", f"Sent back: {comment}", user)
    msg = f"{user.full_name or user.email} sent back the work order for PID {wo.pid}: {comment}"
    if wo.created_by:
        notify([wo.created_by], "Work order sent back", msg, wo)
        _simple_mail([wo.created_by], f"Work order sent back: PID {wo.pid}", "Work order sent back", msg, wo)
    return wo


@transaction.atomic
def approve(wo, user, comment=""):
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if wo.status != WorkOrder.STATUS_IN_REVIEW or not can_approve(user):
        raise WorkOrderError("Only the GM can approve a work order that is waiting for approval.")
    if wo.parent_id and wo.parent.base_number:
        wo.base_number = wo.parent.base_number
        wo.number = f"{wo.base_number}R{wo.revision}"
    else:
        wo.base_number = next_number(wo)
        wo.number = wo.base_number
    wo.status = WorkOrder.STATUS_ISSUED
    wo.approved_by = user
    wo.approved_at = timezone.now()
    wo.gm_comment = (comment or "").strip()
    wo.save()
    if wo.parent_id:
        WorkOrder.objects.filter(pk=wo.parent_id).update(status=WorkOrder.STATUS_REVISED, updated_at=timezone.now())
        event(wo.parent, "revised", f"Replaced by {wo.number}", user)
    event(wo, "approved", f"Approved and issued as {wo.number}" + (f" - {wo.gm_comment}" if wo.gm_comment else ""), user)
    transaction.on_commit(lambda: _after_approval(wo.id))
    return wo


def _after_approval(wo_id):
    from .tasks import issue_work_order
    try:
        issue_work_order.delay(wo_id)
    except Exception as e:   # no broker: do it now
        logger.warning("issue task not queued (%s) - running now", e)
        issue_work_order(wo_id)


def generate_pdf(wo):
    from django.core.files.base import ContentFile
    from .pdf import render_pdf
    data = render_pdf(wo)
    if wo.pdf:
        wo.pdf.delete(save=False)
    wo.pdf.save(f"{wo.number or 'DRAFT-' + str(wo.pk)}.pdf", ContentFile(data), save=False)
    WorkOrder.objects.filter(pk=wo.pk).update(pdf=wo.pdf.name)
    return data


@transaction.atomic
def revise(wo, user):
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if wo.status not in WorkOrder.ACTIVE + (WorkOrder.STATUS_DONE,) or not (can_approve(user) or can_create(user)):
        raise WorkOrderError("Only an issued work order can be revised.")
    if wo.revisions.filter(status__in=WorkOrder.EDITABLE + (WorkOrder.STATUS_IN_REVIEW,)).exists():
        raise WorkOrderError("A revision of this work order is already being prepared.")
    new = WorkOrder.objects.get(pk=wo.pk)
    new.pk = None
    new.number = None
    new.parent = wo
    new.revision = wo.revision + 1
    new.status = WorkOrder.STATUS_DRAFT
    new.created_by = user
    new.submitted_at = new.approved_at = new.done_at = new.closed_at = None
    new.approved_by = None
    new.gm_comment = ""
    new.pdf = ""
    new.save()
    new.assignees.set(wo.assignees.all())
    for ln in wo.lines.all():
        ln.pk = None
        ln.work_order = new
        ln.save()
    for c in wo.checklist.all():
        ChecklistItem.objects.create(work_order=new, stage=c.stage, text=c.text, position=c.position)
    from django.core.files.base import ContentFile
    for a in wo.attachments.filter(by_assignee=False):
        b = Attachment(work_order=new, caption=a.caption, include_in_pdf=a.include_in_pdf, uploaded_by=a.uploaded_by,
                       original_name=a.original_name)
        try:
            with a.file.open("rb") as fh:
                b.file.save(a.filename, ContentFile(fh.read()), save=True)
        except Exception as e:
            logger.warning("copy attachment %s: %s", a.file.name, e)
    event(new, "created", f"Revision {new.revision} of {wo.number} started", user)
    event(wo, "revision", f"Revision {new.revision} started", user)
    return new


@transaction.atomic
def set_status(wo, user, status):
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if status == WorkOrder.STATUS_IN_PROGRESS:
        if wo.status != WorkOrder.STATUS_ISSUED or not can_work(user, wo):
            raise WorkOrderError("Can't start this work order.")
        msg = "Work started"
    elif status == WorkOrder.STATUS_DONE:
        if not can_work(user, wo):
            raise WorkOrderError("Can't mark this work order done.")
        open_items = wo.checklist.filter(done=False).count()
        if open_items:
            raise WorkOrderError(f"Tick all checklist items first ({open_items} open).")
        wo.done_at = timezone.now()
        msg = "Marked done"
    elif status == WorkOrder.STATUS_CLOSED:
        if not can_close(user, wo):
            raise WorkOrderError("Can't close this work order.")
        wo.closed_at = timezone.now()
        msg = "Closed"
    elif status == WorkOrder.STATUS_CANCELLED:
        if wo.status not in WorkOrder.ACTIVE + (WorkOrder.STATUS_DONE,) or not (can_approve(user) or is_admin(user)):
            raise WorkOrderError("Only the GM or an admin can cancel an issued work order.")
        msg = "Cancelled"
    else:
        raise WorkOrderError("Unknown status.")
    wo.status = status
    wo.save()
    event(wo, status.lower(), msg, user)
    if status == WorkOrder.STATUS_DONE:
        people = [u for u in [wo.created_by, wo.approved_by] if u]
        notify(people, f"Work order done: {wo.number}", f"{user.full_name or user.email} marked {wo.number} done.", wo)
    return wo


@transaction.atomic
def tick(wo, user, item_id, done):
    if not can_work(user, wo):
        raise WorkOrderError("You can't update this work order.")
    item = wo.checklist.get(pk=item_id)
    item.done = bool(done)
    item.done_by = user if item.done else None
    item.done_at = timezone.now() if item.done else None
    item.save()
    if wo.status == WorkOrder.STATUS_ISSUED and item.done:
        WorkOrder.objects.filter(pk=wo.pk).update(status=WorkOrder.STATUS_IN_PROGRESS)
    event(wo, "checklist", f"{'Ticked' if item.done else 'Unticked'}: Stage {item.stage} - {item.text}", user)
    return item


@transaction.atomic
def add_progress(wo, user, text, files):
    if not can_work(user, wo):
        raise WorkOrderError("You can't update this work order.")
    text = (text or "").strip()
    if not text and not files:
        raise WorkOrderError("Write a note or attach a photo.")
    if text:
        Note.objects.create(work_order=wo, author=user, text=text)
    _add_files(wo, files, user, in_pdf=False, by_assignee=True)
    event(wo, "progress", "Progress note / photos added", user)
