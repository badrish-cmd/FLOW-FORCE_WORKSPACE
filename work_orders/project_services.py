"""Single (landscape) work order: one work order per PID for all departments.

Draft: the creator adds / removes stages, departments, tasks, checklist points and the specification.
Issued: each employee ticks his items in the workspace (checklist points need a document number, certificates
also the file); the team marks its stage done; when everything is ticked the work order is "Completed -
waiting for GM" and the GM closes it.
"""
import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import project_defaults as D
from . import services as base
from .models import (Attachment, RefDoc, SpecRow, Team, WODepartment, WOItem, WorkOrder, WorkOrderLine, WOStage)

logger = logging.getLogger(__name__)
User = get_user_model()
WorkOrderError = base.WorkOrderError

DEPT_LABELS = {"ENG": "Engineering", "PRC": "Procurement", "WS": "Workshop / QC", "WH": "Warehouse / Logistics",
               "SITE": "Site", "ADM": "Admin"}
LIVE = WorkOrder.ACTIVE  # issued / in progress


def dept_label(team):
    return DEPT_LABELS.get(team.code, team.name)


def person(u):
    """Name - designation (as printed on the work order)."""
    if not u:
        return ""
    name = u.full_name or u.email
    return f"{name} - {u.designation}" if getattr(u, "designation", "") else name


def _user(uid):
    if not str(uid or "").isdigit():
        return None
    return User.objects.filter(pk=int(uid), is_active=True).first()


def _txt(v, n=255):
    return (v or "").strip()[:n]


# ================================================================== create

@transaction.atomic
def create(user, *, wo_type, pid, is_internal, reference, data, lines, pid_head=None):
    if not base.can_create(user):
        raise WorkOrderError("You don't have permission to create work orders.")
    if wo_type not in D.TYPE_NAMES:
        raise WorkOrderError("Pick Project, Supply or Service.")
    wo = WorkOrder(layout=WorkOrder.LAYOUT_PROJECT, wo_type=wo_type, created_by=user, is_internal=is_internal,
                   pid="INT" if is_internal else _txt(pid, 40), reference=_txt(reference))
    if is_internal and not wo.reference:
        raise WorkOrderError("Type the internal reference (what / where the work order is for).")
    if not is_internal and not wo.pid:
        raise WorkOrderError("Pick the PID.")
    if pid_head:
        wo.pid_table_id = pid_head.get("table_id")
        wo.pid_row_ids = pid_head.get("row_ids") or []
    _apply_header(wo, data)
    wo.save()
    _set_lines(wo, lines)
    for i, (num, name, desc, teams, _types) in enumerate(D.stages_for(wo_type)):
        WOStage.objects.create(work_order=wo, position=i, number=num, name=name, description=desc, teams=teams)
    teams = {t.code: t for t in Team.objects.filter(is_active=True)}
    for pos, code in enumerate(D.DEPTS_FOR_TYPE[wo_type]):
        if code in teams:
            _new_department(wo, teams[code], pos, wo_type)
    for i, p in enumerate(D.SPEC_PARAMETERS):
        SpecRow.objects.create(work_order=wo, position=i, parameter=p)
    base.event(wo, "created", f"Draft created ({D.TYPE_NAMES[wo_type]})", user)
    return wo


def _new_department(wo, team, pos, wo_type=None):
    d = WODepartment.objects.create(work_order=wo, team=team, position=pos)
    for i, text in enumerate(D.tasks_for(team.code, wo_type or wo.wo_type)):
        WOItem.objects.create(department=d, kind=WOItem.KIND_TASK, position=i, text=text)
    for i, (text, file_req) in enumerate(D.checks_for(team.code, wo_type or wo.wo_type)):
        WOItem.objects.create(department=d, kind=WOItem.KIND_CHECK, position=i, text=text, file_required=file_req)
    return d


HEADER_TEXT = ("customer_name", "customer_contact", "invoice_address", "delivery_address", "customer_po", "quotation_no",
               "sales_order_no", "delivery_method", "title", "instructions", "notes")


def _apply_header(wo, data):
    for f in HEADER_TEXT:
        if f in data:
            setattr(wo, f, (data.get(f) or "").strip())
    for f in ("order_date", "required_by"):
        if f in data:
            setattr(wo, f, data.get(f) or None)
    if "project_manager" in data:
        wo.project_manager = _user(data.get("project_manager"))
    if data.get("wo_type") in D.TYPE_NAMES:
        wo.wo_type = data["wo_type"]


def _set_lines(wo, lines):
    wo.lines.all().delete()
    for i, ln in enumerate(lines or []):
        desc = (ln.get("description") or "").strip()
        if desc:
            WorkOrderLine.objects.create(work_order=wo, position=i, description=desc, qty=ln.get("qty"),
                                         unit=(ln.get("unit") or "").strip()[:30], source_row_id=ln.get("row_id"),
                                         rate=None, discount=None)


# ================================================================== draft editing

def can_edit(user, wo):
    return wo.is_project and base.can_edit(user, wo)


def _need_edit(user, wo):
    if not can_edit(user, wo):
        raise WorkOrderError("This can be changed only while the work order is a draft. "
                             "After approval, use Revise (it goes to the GM again).")


def sync_assignees(wo):
    """Everyone named on the work order (PICs, members, item assignees) sees it and gets the issue mail."""
    ids = set()
    for d in wo.departments.prefetch_related("members", "items"):
        if d.pic_id:
            ids.add(d.pic_id)
        ids.update(m.id for m in d.members.all())
        ids.update(i.assigned_to_id for i in d.items.all() if i.assigned_to_id)
    wo.assignees.set(User.objects.filter(pk__in=ids))


@transaction.atomic
def update_header(wo, user, data, lines):
    wo.refresh_from_db()
    _need_edit(user, wo)
    if wo.is_internal:
        wo.reference = _txt(data.get("reference")) or wo.reference
    _apply_header(wo, data)
    wo.save()
    _set_lines(wo, lines)
    base.event(wo, "edited", "Work order details edited", user)


def _stage_data(st, data):
    st.name = _txt(data.get("name")) or st.name
    st.description = _txt(data.get("description"))
    st.teams = [c for c in (data.get("teams") or []) if c]
    st.target_date = data.get("target_date") or None


@transaction.atomic
def add_stage(wo, user, data):
    wo.refresh_from_db()
    _need_edit(user, wo)
    if not _txt(data.get("name")):
        raise WorkOrderError("Type the stage name.")
    last = wo.wo_stages.order_by("-position").first()
    st = WOStage(work_order=wo, position=(last.position + 1) if last else 0,
                 number=(max([s.number for s in wo.wo_stages.all()] or [0]) + 1))
    _stage_data(st, data)
    st.save()
    base.event(wo, "stage", f"Stage added: {st.name}", user)
    return st


@transaction.atomic
def update_stage(wo, user, stage_id, data):
    wo.refresh_from_db()
    _need_edit(user, wo)
    st = wo.wo_stages.get(pk=stage_id)
    _stage_data(st, data)
    st.save()
    return st


@transaction.atomic
def delete_stage(wo, user, stage_id):
    wo.refresh_from_db()
    _need_edit(user, wo)
    st = wo.wo_stages.get(pk=stage_id)
    base.event(wo, "stage", f"Stage removed: {st.name}", user)
    st.delete()


@transaction.atomic
def add_department(wo, user, team_id):
    wo.refresh_from_db()
    _need_edit(user, wo)
    team = Team.objects.filter(pk=team_id, is_active=True).first() if str(team_id or "").isdigit() else None
    if not team:
        raise WorkOrderError("Pick the department.")
    if wo.departments.filter(team=team).exists():
        raise WorkOrderError(f"{dept_label(team)} is already on this work order.")
    last = wo.departments.order_by("-position").first()
    d = _new_department(wo, team, (last.position + 1) if last else 0)
    base.event(wo, "department", f"Department added: {dept_label(team)}", user)
    return d


@transaction.atomic
def update_department(wo, user, dept_id, *, pic_id, member_ids, target_date):
    wo.refresh_from_db()
    _need_edit(user, wo)
    d = wo.departments.get(pk=dept_id)
    d.pic = _user(pic_id)
    d.target_date = target_date or None
    d.save()
    d.members.set(User.objects.filter(pk__in=[int(i) for i in member_ids if str(i).isdigit()], is_active=True))
    sync_assignees(wo)
    return d


@transaction.atomic
def delete_department(wo, user, dept_id):
    wo.refresh_from_db()
    _need_edit(user, wo)
    d = wo.departments.get(pk=dept_id)
    base.event(wo, "department", f"Department removed: {dept_label(d.team)}", user)
    d.delete()
    sync_assignees(wo)


# ---------------------------------------------------------------- items (tasks + checklist points)

def can_add_item(user, wo, dept):
    """Draft: the creator. After issue: the department PIC, coordinators and admins (more work came up)."""
    if can_edit(user, wo):
        return True
    return wo.status in LIVE and (base.is_coordinator(user) or dept.pic_id == user.id)


@transaction.atomic
def add_item(wo, user, dept_id, *, kind, text, assigned_to_id=None, file_required=False, target_date=None):
    wo.refresh_from_db()
    d = wo.departments.get(pk=dept_id)
    if not can_add_item(user, wo, d):
        raise WorkOrderError("You can't add items to this department.")
    text = _txt(text)
    if not text:
        raise WorkOrderError("Type the task / checklist point.")
    kind = WOItem.KIND_CHECK if kind == WOItem.KIND_CHECK else WOItem.KIND_TASK
    last = d.items.filter(kind=kind).order_by("-position").first()
    it = WOItem.objects.create(department=d, kind=kind, text=text, position=(last.position + 1) if last else 0,
                               assigned_to=_user(assigned_to_id), file_required=bool(file_required) and kind == WOItem.KIND_CHECK,
                               target_date=target_date or None, added_after_issue=wo.status in LIVE)
    sync_assignees(wo)
    base.event(wo, "item", f"Added to {dept_label(d.team)}: {it.text}", user)
    if wo.status in LIVE and it.assigned_to and it.assigned_to != user:
        base.notify([it.assigned_to], f"New task on {wo.number}", f"{user.full_name or user.email} added: {it.text}", wo)
    return it


@transaction.atomic
def update_item(wo, user, item_id, *, text, assigned_to_id=None, file_required=False, target_date=None):
    wo.refresh_from_db()
    it = WOItem.objects.select_related("department").get(pk=item_id, department__work_order=wo)
    if not (can_edit(user, wo) or (wo.status in LIVE and base.is_admin(user))):
        raise WorkOrderError("Only drafts can be changed (admins can still fix an issued work order).")
    it.text = _txt(text) or it.text
    it.assigned_to = _user(assigned_to_id)
    it.file_required = bool(file_required) and it.is_check
    it.target_date = target_date or None
    it.save()
    sync_assignees(wo)
    return it


@transaction.atomic
def delete_item(wo, user, item_id):
    wo.refresh_from_db()
    it = WOItem.objects.select_related("department").get(pk=item_id, department__work_order=wo)
    if not (can_edit(user, wo) or (wo.status in LIVE and base.is_admin(user))):
        raise WorkOrderError("Only drafts can be changed (admins can still fix an issued work order).")
    base.event(wo, "item", f"Removed from {dept_label(it.department.team)}: {it.text}", user)
    it.delete()
    sync_assignees(wo)
    if wo.status in LIVE:
        check_complete(wo, user)


# ================================================================== page 2: specification

def can_edit_spec(user, wo):
    if not wo.is_project or wo.spec_status == WorkOrder.SPEC_CONFIRMED:
        return False
    if can_edit(user, wo):
        return True
    return base.is_spec_editor(user) and wo.status in WorkOrder.EDITABLE + LIVE + (WorkOrder.STATUS_IN_REVIEW,)


def can_confirm_spec(user, wo):
    return (wo.is_project and wo.spec_status == WorkOrder.SPEC_PENDING and base.is_spec_editor(user)
            and wo.status not in (WorkOrder.STATUS_CLOSED, WorkOrder.STATUS_CANCELLED, WorkOrder.STATUS_REVISED))


@transaction.atomic
def save_spec(wo, user, rows, docs):
    wo.refresh_from_db()
    if not can_edit_spec(user, wo):
        raise WorkOrderError("The specification is confirmed or you can't edit it. A change after confirmation "
                             "is made as a revision.")
    wo.spec_rows.all().delete()
    for i, r in enumerate(rows):
        if not any((r.get(k) or "").strip() for k in ("parameter", "requirement", "final")):
            continue
        req, fin = (r.get("requirement") or "").strip(), (r.get("final") or "").strip()
        SpecRow.objects.create(work_order=wo, position=i, parameter=_txt(r.get("parameter"), 120) or "-",
                               requirement=req, final=fin, reason=_txt(r.get("reason")),
                               changed=bool(r.get("changed")) or bool(req and fin and req != fin))
    wo.ref_docs.all().delete()
    for i, r in enumerate(docs):
        if not _txt(r.get("document")):
            continue
        RefDoc.objects.create(work_order=wo, position=i, document=_txt(r.get("document")), reference=_txt(r.get("reference")),
                              revision=_txt(r.get("revision"), 40), doc_date=r.get("doc_date") or None,
                              source=_txt(r.get("source"), 120))
    base.event(wo, "spec", "Specification (page 2) saved", user)


@transaction.atomic
def confirm_spec(wo, user, *, customer_by, customer_date, method):
    wo = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if not can_confirm_spec(user, wo):
        raise WorkOrderError("Only a Spec editor can confirm the specification.")
    if not _txt(customer_by) or not customer_date:
        raise WorkOrderError("Type who confirmed it at the customer (name - designation) and the date.")
    if not wo.spec_rows.exclude(final="").exists():
        raise WorkOrderError("Fill the final confirmed specification first.")
    wo.spec_customer_by, wo.spec_customer_date, wo.spec_customer_method = _txt(customer_by), customer_date, _txt(method, 120)
    wo.spec_confirmed_by, wo.spec_confirmed_at = user, timezone.now()
    wo.spec_status = WorkOrder.SPEC_CONFIRMED
    wo.save()
    base.event(wo, "spec", f"Specification confirmed (customer: {wo.spec_customer_by})", user)
    if wo.status in LIVE:
        base.notify(list(wo.assignees.all()) + base.approvers(), f"Specification confirmed: {wo.display_number}",
                    "The final specification is confirmed - work to page 2.", wo)
    return wo


# ================================================================== working: ticks

def _in_dept(user, d):
    return d.pic_id == user.id or d.members.filter(pk=user.pk).exists()


def can_tick(user, item):
    wo = item.department.work_order
    if wo.status not in LIVE or item.state != WOItem.STATE_OPEN:
        return False
    if base.is_coordinator(user):
        return True
    d = item.department
    if d.pic_id == user.id or item.assigned_to_id == user.id:
        return True
    return item.assigned_to_id is None and d.members.filter(pk=user.pk).exists()


def can_mark_stage(user, stage):
    wo = stage.work_order
    if wo.status not in LIVE or stage.done:
        return False
    if base.is_coordinator(user):
        return True
    return any(_in_dept(user, d) for d in wo.departments.filter(team__code__in=stage.teams or []))


def _item(wo, item_id):
    return WOItem.objects.select_related("department__team", "department__work_order").get(pk=item_id, department__work_order=wo)


def _started(wo):
    if WorkOrder.objects.filter(pk=wo.pk, status=WorkOrder.STATUS_ISSUED).update(status=WorkOrder.STATUS_IN_PROGRESS):
        wo.status = WorkOrder.STATUS_IN_PROGRESS


@transaction.atomic
def tick_item(wo, user, item_id, *, doc_ref="", file=None):
    wo.refresh_from_db()
    it = _item(wo, item_id)
    if not can_tick(user, it):
        raise WorkOrderError("You can't tick this item.")
    doc_ref = _txt(doc_ref)
    if it.is_check and not doc_ref:
        raise WorkOrderError("Type the document number for this checklist point.")
    if it.file_required and not file and not it.attachment_id:
        raise WorkOrderError("Attach the certificate / document file for this point.")
    if file:
        base.validate_file(file)
        a = Attachment(work_order=wo, uploaded_by=user, by_assignee=True, include_in_pdf=False,
                       original_name=os.path.basename(file.name)[:255], caption=f"{it.text} - {doc_ref}"[:255])
        a.file.save(os.path.basename(file.name), file, save=True)
        it.attachment = a
    it.state, it.doc_ref, it.done_by, it.done_at, it.na_reason = WOItem.STATE_DONE, doc_ref, user, timezone.now(), ""
    it.save()
    _started(wo)
    msg = f"Ticked: {it.text}" + (f" [{doc_ref}]" if doc_ref else "")
    base.event(wo, "tick", f"{dept_label(it.department.team)} - {msg}", user)
    _tick_mail(wo, it.department, user, f"{dept_label(it.department.team)}: {msg}")
    check_complete(wo, user)
    return it


@transaction.atomic
def na_item(wo, user, item_id, reason):
    wo.refresh_from_db()
    it = _item(wo, item_id)
    if not can_tick(user, it):
        raise WorkOrderError("You can't change this item.")
    reason = _txt(reason)
    if not reason:
        raise WorkOrderError("Write why this point is not applicable.")
    it.state, it.na_reason, it.done_by, it.done_at = WOItem.STATE_NA, reason, user, timezone.now()
    it.save()
    _started(wo)
    base.event(wo, "tick", f"{dept_label(it.department.team)} - N/A: {it.text} ({reason})", user)
    _tick_mail(wo, it.department, user, f"{dept_label(it.department.team)}: marked N/A - {it.text} (reason: {reason})")
    check_complete(wo, user)
    return it


def _reopen_check(user, wo):
    if not base.is_admin(user):
        raise WorkOrderError("Only an admin can untick.")
    if wo.status not in LIVE + (WorkOrder.STATUS_DONE,):
        raise WorkOrderError("This work order is not open.")


@transaction.atomic
def reopen_item(wo, user, item_id):
    wo.refresh_from_db()
    _reopen_check(user, wo)
    it = _item(wo, item_id)
    it.state, it.done_by, it.done_at, it.na_reason = WOItem.STATE_OPEN, None, None, ""
    it.save()
    _back_in_progress(wo)
    base.event(wo, "tick", f"Unticked: {dept_label(it.department.team)} - {it.text}", user)
    return it


@transaction.atomic
def mark_stage(wo, user, stage_id):
    wo.refresh_from_db()
    st = wo.wo_stages.get(pk=stage_id)
    if not can_mark_stage(user, st):
        raise WorkOrderError("Only the responsible team (or a coordinator) can mark this stage done.")
    st.done, st.done_by, st.done_at = True, user, timezone.now()
    st.save()
    _started(wo)
    base.event(wo, "stage", f"Stage {st.number} completed: {st.name}", user)
    _stage_mail(wo, st, user)
    check_complete(wo, user)
    return st


@transaction.atomic
def reopen_stage(wo, user, stage_id):
    wo.refresh_from_db()
    _reopen_check(user, wo)
    st = wo.wo_stages.get(pk=stage_id)
    st.done, st.done_by, st.done_at = False, None, None
    st.save()
    _back_in_progress(wo)
    base.event(wo, "stage", f"Stage {st.number} reopened: {st.name}", user)
    return st


def _back_in_progress(wo):
    if wo.status == WorkOrder.STATUS_DONE:
        WorkOrder.objects.filter(pk=wo.pk).update(status=WorkOrder.STATUS_IN_PROGRESS, done_at=None)
        wo.status = WorkOrder.STATUS_IN_PROGRESS


def progress(wo):
    items = list(WOItem.objects.filter(department__work_order=wo).values_list("state", flat=True))
    stages = list(wo.wo_stages.values_list("done", flat=True))
    total = len(items) + len(stages)
    closed = sum(1 for s in items if s != WOItem.STATE_OPEN) + sum(1 for d in stages if d)
    return {"total": total, "closed": closed, "pct": int(round(100 * closed / total)) if total else 0,
            "items_open": sum(1 for s in items if s == WOItem.STATE_OPEN), "stages_open": sum(1 for d in stages if not d)}


def check_complete(wo, user):
    wo.refresh_from_db(fields=["status"])
    if wo.status not in LIVE:
        return False
    p = progress(wo)
    if p["total"] == 0 or p["items_open"] or p["stages_open"]:
        return False
    WorkOrder.objects.filter(pk=wo.pk).update(status=WorkOrder.STATUS_DONE, done_at=timezone.now(), updated_at=timezone.now())
    wo.status = WorkOrder.STATUS_DONE
    base.event(wo, "done", "All stages and items ticked - completed, waiting for GM to close", user)
    people = base.approvers() + ([wo.created_by] if wo.created_by else [])
    msg = f"Every stage and checklist item of {wo.display_number} ({wo.pid_label}) is ticked. Please review and close it."
    base.notify(people, f"Work order completed: {wo.display_number}", msg, wo)
    _mail(_emails(people), f"Completed - please close: {wo.display_number}", "Work order completed", msg, wo)
    return True


# ================================================================== emails

def _emails(users, extra=()):
    out = []
    for e in [u.email for u in users if u and u.is_active and u.email] + list(extra):
        e = (e or "").strip().lower()
        if e and e not in out:
            out.append(e)
    return out


def _mail(addresses, subject, title, message, wo, lines=None):
    from django.template.loader import render_to_string
    from django.urls import reverse
    from tasks.models import EmailLog
    from tasks.tasks import send_email_log_task
    url = base._site() + reverse("work_orders:detail", args=[wo.id])
    for email in addresses:
        html = render_to_string("work_orders/email.html", {"name": "", "title": title, "message": message, "wo": wo,
                                                           "url": url, "files": [], "lines": lines or [],
                                                           "short": True})
        log = EmailLog.objects.create(recipient_email=email, subject=f"[Flow-Force] {subject}"[:255], body=html,
                                      email_type="WORK_ORDER_MAIL", status="PENDING", max_retries=3)
        transaction.on_commit(lambda i=log.id: send_email_log_task.delay(i))


def _tick_mail(wo, dept, user, what):
    """Every tick: GM, the department PIC, automation@ and operations@ (WORK_ORDER_TICK_EMAILS)."""
    people = base.approvers() + ([dept.pic] if dept.pic else [])
    msg = f"{user.full_name or user.email} - {what}"
    _mail(_emails(people, settings.WORK_ORDER_TICK_EMAILS), f"{wo.display_number} - {what}"[:200], "Work order update",
          msg, wo)


def _stage_mail(wo, st, user):
    """Stage completed: creator + GM, plus the tick list (PICs of the stage's teams, automation@, operations@)."""
    pics = [d.pic for d in wo.departments.filter(team__code__in=st.teams or []).select_related("pic") if d.pic]
    people = base.approvers() + ([wo.created_by] if wo.created_by else []) + pics
    msg = f"{user.full_name or user.email} marked stage {st.number} - {st.name} as completed."
    _mail(_emails(people, settings.WORK_ORDER_TICK_EMAILS), f"{wo.display_number} - Stage {st.number} completed: {st.name}",
          "Stage completed", msg, wo)


def overdue_alerts(today=None):
    """Daily: one mail per person listing his overdue items / stages (assignee, department PIC, creator)."""
    today = today or timezone.localdate()
    sent = 0
    qs = (WorkOrder.objects.filter(layout=WorkOrder.LAYOUT_PROJECT, status__in=LIVE)
          .exclude(overdue_alerted_on=today).select_related("created_by"))
    for wo in qs:
        per = {}

        def add(users, line):
            for u in users:
                if u and u.is_active and u.email:
                    per.setdefault(u.email.lower(), []).append(line)
        for it in WOItem.objects.filter(department__work_order=wo, state=WOItem.STATE_OPEN).select_related(
                "department__team", "department__pic", "assigned_to"):
            due = it.due_date
            if not due or due >= today:
                continue
            line = f"{dept_label(it.department.team)}: {it.text} (target {due:%d/%m/%Y})"
            who = [it.assigned_to] if it.assigned_to else list(it.department.members.all())
            add(who + [it.department.pic, wo.created_by], line)
        for st in wo.wo_stages.filter(done=False, target_date__lt=today):
            line = f"Stage {st.number} - {st.name} (target {st.target_date:%d/%m/%Y})"
            pics = [d.pic for d in wo.departments.filter(team__code__in=st.teams or []).select_related("pic")]
            add(pics + [wo.created_by], line)
        for email, lines in per.items():
            _mail([email], f"Overdue on {wo.display_number} - {wo.pid_label}", "Overdue items",
                  f"These items on {wo.display_number} are past their target date:", wo, lines=lines)
            sent += 1
        WorkOrder.objects.filter(pk=wo.pk).update(overdue_alerted_on=today)
    return sent


# ================================================================== ready check, revisions

def check_ready(wo):
    problems = []
    if not wo.wo_stages.exists():
        problems.append("Add at least one stage.")
    depts = list(wo.departments.select_related("team"))
    if not depts:
        problems.append("Add at least one department.")
    no_pic = [dept_label(d.team) for d in depts if not d.pic_id]
    if no_pic:
        problems.append("Pick the person in charge (PIC) for: " + ", ".join(no_pic) + ".")
    if not wo.lines.exists():
        problems.append("Add the scope of work / items.")
    return problems


def revise_project(wo, user):
    """Copy everything (ticks kept) into a new draft R(n+1); it goes to the GM again."""
    from django.core.files.base import ContentFile
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
    new.spec_status = WorkOrder.SPEC_PENDING     # the revision is usually for a spec change
    new.spec_confirmed_by, new.spec_confirmed_at = None, None
    new.overdue_alerted_on = None
    new.save()
    for ln in wo.lines.all():
        ln.pk, ln.work_order = None, new
        ln.save()
    att_map = {}
    for a in wo.attachments.all():
        b = Attachment(work_order=new, caption=a.caption, include_in_pdf=a.include_in_pdf, uploaded_by=a.uploaded_by,
                       original_name=a.original_name, by_assignee=a.by_assignee)
        try:
            with a.file.open("rb") as fh:
                b.file.save(a.filename, ContentFile(fh.read()), save=True)
            att_map[a.id] = b
        except Exception as e:
            logger.warning("copy attachment %s: %s", a.file.name, e)
    for st in wo.wo_stages.all():
        st.pk, st.work_order = None, new
        st.save()
    for d in wo.departments.prefetch_related("members", "items"):
        members = list(d.members.all())
        items = list(d.items.all())
        nd = WODepartment.objects.create(work_order=new, team=d.team, position=d.position, pic=d.pic, target_date=d.target_date)
        nd.members.set(members)
        for it in items:
            att = att_map.get(it.attachment_id)
            it.pk, it.department, it.attachment = None, nd, att
            it.save()
    for r in wo.spec_rows.all():
        r.pk, r.work_order = None, new
        r.save()
    for r in wo.ref_docs.all():
        r.pk, r.work_order = None, new
        r.save()
    sync_assignees(new)
    base.event(new, "created", f"Revision {new.revision} of {wo.number} started (ticks kept)", user)
    base.event(wo, "revision", f"Revision {new.revision} started", user)
    return new


def carry_over_ticks(old, new):
    """On approval of a revision: anything ticked on the old one while the revision was waiting is kept."""
    old_items = {(i.department.team_id, i.kind, i.text): i for i in
                 WOItem.objects.filter(department__work_order=old).select_related("department")}
    for it in WOItem.objects.filter(department__work_order=new, state=WOItem.STATE_OPEN).select_related("department"):
        o = old_items.get((it.department.team_id, it.kind, it.text))
        if o and o.state != WOItem.STATE_OPEN:
            it.state, it.doc_ref, it.na_reason, it.done_by, it.done_at = o.state, o.doc_ref, o.na_reason, o.done_by, o.done_at
            it.save()
    old_stages = {s.name: s for s in old.wo_stages.all()}
    for st in new.wo_stages.filter(done=False):
        o = old_stages.get(st.name)
        if o and o.done:
            st.done, st.done_by, st.done_at = True, o.done_by, o.done_at
            st.save()


# ================================================================== my tasks

def my_open_items(user):
    """Open items the user can tick (assigned to him, or his department's unassigned items, or as PIC)."""
    from django.db.models import Q
    return (WOItem.objects.filter(state=WOItem.STATE_OPEN, department__work_order__status__in=LIVE,
                                  department__work_order__layout=WorkOrder.LAYOUT_PROJECT)
            .filter(Q(assigned_to=user) | Q(department__pic=user) | Q(assigned_to__isnull=True, department__members=user))
            .select_related("department__team", "department__work_order", "assigned_to").distinct()
            .order_by("department__work_order__id", "department__position", "kind", "position"))


def my_open_stages(user):
    from django.db.models import Q
    out = []
    depts = WODepartment.objects.filter(work_order__status__in=LIVE, work_order__layout=WorkOrder.LAYOUT_PROJECT).filter(
        Q(pic=user) | Q(members=user)).select_related("team", "work_order").distinct()
    seen = set()
    for d in depts:
        for st in d.work_order.wo_stages.filter(done=False):
            if d.team.code in (st.teams or []) and st.id not in seen:
                seen.add(st.id)
                out.append(st)
    return out
