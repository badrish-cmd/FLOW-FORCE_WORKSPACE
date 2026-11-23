import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import pid as pid_svc
from . import services
from .models import (SECTIONS, STAGES, Attachment, Team, WorkOrder, WorkOrderAccess, WorkOrderAccessChange)

logger = logging.getLogger(__name__)
User = get_user_model()
NO_ACCESS = "You don't have access to Work Orders."


def _forbid(msg=NO_ACCESS):
    return HttpResponseForbidden(msg)


def _get(request, pk):
    wo = get_object_or_404(WorkOrder.objects.select_related("team", "created_by", "approved_by", "parent"), pk=pk)
    if not services.can_see(request.user, wo):
        raise Http404
    return wo


def _ctx(request, **kw):
    kw.update({"wo_is_admin": services.is_admin(request.user), "wo_can_create": services.can_create(request.user),
               "wo_can_approve": services.can_approve(request.user), "wo_sees_all": services.sees_all(request.user),
               "statuses": WorkOrder.STATUS_CHOICES, "today": timezone.localdate()})
    return kw


def _dec(s):
    s = (s or "").strip().replace(",", "")
    try:
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


def _date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


@login_required
def wo_list(request):
    if not services.can_use(request.user):
        return _forbid()
    base = services.visible_qs(request.user)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    team = request.GET.get("team", "")
    mine = request.GET.get("mine", "")
    if not services.sees_all(request.user):
        mine = "1"
    if q:
        base = base.filter(Q(number__icontains=q) | Q(pid__icontains=q) | Q(customer_name__icontains=q) | Q(reference__icontains=q)
                           | Q(customer_po__icontains=q) | Q(job_no__icontains=q) | Q(title__icontains=q))
    if team:
        base = base.filter(team__code=team)
    if mine:
        base = base.filter(assignees=request.user)
    counts = {r["status"]: r["n"] for r in base.values("status").annotate(n=Count("id", distinct=True))}
    qs = base
    if status == "open":
        qs = qs.filter(status__in=WorkOrder.ACTIVE)
    elif status == "overdue":
        qs = qs.filter(status__in=WorkOrder.ACTIVE, required_by__lt=timezone.localdate())
    elif status:
        qs = qs.filter(status=status)
    cards = [
        ("", "All", sum(counts.values())),
        ("DRAFT", "Draft", counts.get("DRAFT", 0) + counts.get("SENT_BACK", 0)),
        ("IN_REVIEW", "Waiting for approval", counts.get("IN_REVIEW", 0)),
        ("open", "Issued / in progress", counts.get("ISSUED", 0) + counts.get("IN_PROGRESS", 0)),
        ("overdue", "Overdue", base.filter(status__in=WorkOrder.ACTIVE, required_by__lt=timezone.localdate()).count()),
        ("DONE", "Done", counts.get("DONE", 0)),
        ("CLOSED", "Closed", counts.get("CLOSED", 0)),
    ]
    page = Paginator(qs.distinct().order_by("-updated_at"), 50).get_page(request.GET.get("page"))
    return render(request, "work_orders/list.html", _ctx(
        request, page=page, q=q, status=status, team=team, mine=mine, cards=cards,
        teams=Team.objects.filter(is_active=True)))


def _form_ctx(request, wo, prefill=None):
    teams = list(Team.objects.filter(is_active=True))
    team_defaults = {t.id: {"sections": t.default_sections, "stages": t.default_stages} for t in teams}
    return _ctx(request, wo=wo, p=prefill or {}, teams=teams, team_defaults=json.dumps(team_defaults),
                sections=SECTIONS, stages=STAGES, employees=services.employees(),
                recent_pids=pid_svc.recent_pids(), pid_found=bool(prefill))


def _prefill_from_wo(wo):
    return {
        "pid": wo.pid, "internal": wo.is_internal, "reference": wo.reference, "team_id": wo.team_id, "customer_name": wo.customer_name, "invoice_address": wo.invoice_address,
        "delivery_address": wo.delivery_address, "order_date": wo.order_date, "required_by": wo.required_by,
        "customer_po": wo.customer_po, "quotation_no": wo.quotation_no, "sales_order_no": wo.sales_order_no,
        "job_no": wo.job_no, "delivery_method": wo.delivery_method, "title": wo.title, "instructions": wo.instructions,
        "notes": wo.notes, "terms": wo.terms, "ppn_percent": wo.ppn_percent,
        "lines": [{"description": l.description, "qty": l.qty, "unit": l.unit, "rate": l.rate, "discount": l.discount,
                   "row_id": l.source_row_id} for l in wo.lines.all()],
        "sections": wo.sections, "stages": wo.stages, "assignee_ids": [u.id for u in wo.assignees.all()],
    }


def _read_post(request):
    P = request.POST
    data = {k: P.get(k, "") for k in ("customer_name", "invoice_address", "delivery_address", "customer_po",
                                     "quotation_no", "sales_order_no", "job_no", "delivery_method", "title",
                                     "instructions", "notes", "terms")}
    data["order_date"] = _date(P.get("order_date"))
    data["required_by"] = _date(P.get("required_by"))
    data["ppn_percent"] = _dec(P.get("ppn_percent")) or Decimal(11)
    lines = []
    for desc, qty, unit, rate, disc, row in zip(P.getlist("line_desc"), P.getlist("line_qty"), P.getlist("line_unit"),
                                                P.getlist("line_rate"), P.getlist("line_disc"), P.getlist("line_row")):
        lines.append({"description": desc, "qty": _dec(qty), "unit": unit, "rate": _dec(rate), "discount": _dec(disc),
                      "row_id": int(row) if row.isdigit() else None})
    return data, lines, P.getlist("sections"), P.getlist("stages"), P.getlist("assignees")


def _save(request, wo, template_prefill):
    data, lines, sections, stages, assignees = _read_post(request)
    team = Team.objects.filter(pk=request.POST.get("team_id"), is_active=True).first()
    if not team:
        messages.error(request, "Pick the team.")
        return None
    wo.team = team
    if not wo.pk:
        if request.POST.get("internal") == "1":
            wo.is_internal = True
            wo.pid = "INT"
        else:
            wo.pid = request.POST.get("pid", "").strip()
            head = pid_svc.load_pid(wo.pid)
            if head:
                wo.pid_table_id = head["table_id"]
                wo.pid_row_ids = head["row_ids"]
    if wo.is_internal:
        wo.reference = request.POST.get("reference", "").strip()[:255]
        if not wo.reference:
            messages.error(request, "Type the internal reference (what / where the work order is for).")
            return None
    try:
        wo = services.save_draft(wo, data, request.user, lines=lines, stages=stages, sections=sections,
                                 assignee_ids=assignees, files=request.FILES.getlist("files"))
        captions = request.POST.getlist("new_captions")
        if captions:
            new = list(wo.attachments.order_by("-id")[:len(request.FILES.getlist("files"))])[::-1]
            for a, c in zip(new, captions):
                if c.strip():
                    a.caption = c.strip()[:255]
                    a.save(update_fields=["caption"])
        if request.POST.get("action") == "submit":
            services.submit(wo, request.user)
            messages.success(request, "Saved and sent to GM for approval.")
        else:
            messages.success(request, "Draft saved.")
    except services.WorkOrderError as e:
        messages.error(request, str(e))
        if wo.pk:
            return wo
        return None
    return wo


@login_required
def wo_create(request):
    if not services.can_create(request.user):
        return _forbid("You don't have permission to create work orders.")
    if request.method == "POST":
        wo = _save(request, WorkOrder(), None)
        if wo:
            return redirect("work_orders:detail", wo.pk)
        data, lines, sections, stages, assignees = _read_post(request)
        prefill = dict(data, pid=request.POST.get("pid", ""), internal=request.POST.get("internal") == "1",
                       reference=request.POST.get("reference", ""), lines=lines, sections=sections,
                       stages=[int(s) for s in stages if s.isdigit()], assignee_ids=[int(a) for a in assignees if a.isdigit()],
                       team_id=int(request.POST.get("team_id")) if (request.POST.get("team_id") or "").isdigit() else None)
        return render(request, "work_orders/form.html", _form_ctx(request, None, prefill))
    pid = request.GET.get("pid", "").strip()
    head = None
    not_found = ""
    if request.GET.get("internal") == "1":
        team = Team.objects.filter(code=request.GET.get("team", "")).first() or Team.objects.filter(is_active=True).first()
        head = {"pid": "INT", "internal": True, "reference": request.GET.get("ref", "").strip(),
                "customer_name": "PT. Flow Force Indonesia", "team_id": team.id if team else None,
                "sections": [s for s in (team.default_sections if team else []) if s not in ("customer_po", "quotation_no", "sales_order")],
                "stages": team.default_stages if team else [], "ppn_percent": 11,
                "lines": [{"description": "", "qty": None, "unit": ""}]}
    elif pid:
        head = pid_svc.load_pid(pid)
        if not head:
            not_found = pid
            messages.error(request, f"PID {pid} not found in LIST PID - PT FLOW FORCE INDONESIA.")
        else:
            team = Team.objects.filter(code=request.GET.get("team", "")).first() or Team.objects.filter(is_active=True).first()
            head.update({"team_id": team.id if team else None,
                         "sections": team.default_sections if team else [], "stages": team.default_stages if team else [],
                         "terms": "", "ppn_percent": 11, "job_no": "", "delivery_method": ""})
    ctx = _form_ctx(request, None, head)
    ctx["not_found"] = not_found
    ctx["teams_get"] = request.GET.get("team", "")
    return render(request, "work_orders/form.html", ctx)


@login_required
def wo_edit(request, pk):
    wo = _get(request, pk)
    if not services.can_edit(request.user, wo):
        messages.error(request, "This work order can't be edited now.")
        return redirect("work_orders:detail", wo.pk)
    if request.method == "POST":
        out = _save(request, wo, None)
        return redirect("work_orders:detail", (out or wo).pk)
    return render(request, "work_orders/form.html", _form_ctx(request, wo, _prefill_from_wo(wo)))


@login_required
def wo_detail(request, pk):
    wo = _get(request, pk)
    u = request.user
    return render(request, "work_orders/detail.html", _ctx(
        request, wo=wo, lines=wo.lines.all(), checklist=wo.checklist.select_related("done_by"),
        attachments=wo.attachments.select_related("uploaded_by"), notes_list=wo.progress_notes.select_related("author"),
        events=wo.events.select_related("user")[:60], revisions=wo.revisions.all(),
        can_edit=services.can_edit(u, wo), can_delete=services.can_delete(u, wo),
        can_submit=services.can_edit(u, wo), can_decide=wo.status == WorkOrder.STATUS_IN_REVIEW and services.can_approve(u),
        can_work=services.can_work(u, wo), can_close=services.can_close(u, wo),
        can_cancel=wo.status in WorkOrder.ACTIVE + (WorkOrder.STATUS_DONE,) and (services.can_approve(u) or services.is_admin(u)),
        can_revise=wo.status in WorkOrder.ACTIVE + (WorkOrder.STATUS_DONE,) and (services.can_approve(u) or services.can_create(u)),
        stage_names=dict(STAGES), section_labels=[l for k, l in SECTIONS if k in (wo.sections or [])]))


def _act(request, pk, fn, ok):
    wo = _get(request, pk)
    try:
        out = fn(wo)
    except services.WorkOrderError as e:
        messages.error(request, str(e))
        return redirect("work_orders:detail", wo.pk)
    if ok:
        messages.success(request, ok if isinstance(ok, str) else ok(out))
    return redirect("work_orders:detail", getattr(out, "pk", wo.pk) or wo.pk)


@login_required
@require_POST
def wo_submit(request, pk):
    return _act(request, pk, lambda wo: services.submit(wo, request.user), "Sent to GM for approval.")


@login_required
@require_POST
def wo_approve(request, pk):
    return _act(request, pk, lambda wo: services.approve(wo, request.user, request.POST.get("comment", "")),
                lambda wo: f"Approved as {wo.number}. The PDF is being made and emailed to the team.")


@login_required
@require_POST
def wo_send_back(request, pk):
    return _act(request, pk, lambda wo: services.send_back(wo, request.user, request.POST.get("comment", "")), "Sent back.")


@login_required
@require_POST
def wo_delete(request, pk):
    wo = _get(request, pk)
    try:
        services.delete_draft(wo, request.user)
    except services.WorkOrderError as e:
        messages.error(request, str(e))
        return redirect("work_orders:detail", wo.pk)
    messages.success(request, "Draft deleted.")
    return redirect("work_orders:list")


@login_required
@require_POST
def wo_revise(request, pk):
    wo = _get(request, pk)
    try:
        new = services.revise(wo, request.user)
    except services.WorkOrderError as e:
        messages.error(request, str(e))
        return redirect("work_orders:detail", wo.pk)
    messages.success(request, f"Revision {new.revision} draft created - edit it and send to GM.")
    return redirect("work_orders:edit", new.pk)


@login_required
@require_POST
def wo_status(request, pk):
    status = request.POST.get("status", "")
    labels = {"IN_PROGRESS": "Started.", "DONE": "Marked done.", "CLOSED": "Closed.", "CANCELLED": "Cancelled."}
    return _act(request, pk, lambda wo: services.set_status(wo, request.user, status), labels.get(status, "Updated."))


@login_required
@require_POST
def wo_tick(request, pk, item_id):
    return _act(request, pk, lambda wo: services.tick(wo, request.user, item_id, request.POST.get("done") == "1"), None)


@login_required
@require_POST
def wo_progress(request, pk):
    return _act(request, pk, lambda wo: services.add_progress(wo, request.user, request.POST.get("text", ""),
                                                             request.FILES.getlist("files")), "Added.")


@login_required
@require_POST
def attachment_update(request, pk, att_id):
    wo = _get(request, pk)
    a = get_object_or_404(Attachment, pk=att_id, work_order=wo)
    if not services.can_edit(request.user, wo):
        return _forbid("Files can be changed only while the work order is a draft.")
    if request.POST.get("delete") == "1":
        name = a.filename
        a.file.delete(save=False)
        a.delete()
        services.event(wo, "file", f"Removed {name}", request.user)
    else:
        a.caption = request.POST.get("caption", "").strip()[:255]
        a.include_in_pdf = request.POST.get("include_in_pdf") == "1"
        a.save(update_fields=["caption", "include_in_pdf"])
    return redirect(reverse("work_orders:detail", args=[wo.pk]) + "#files")


@login_required
def attachment_file(request, pk, att_id):
    wo = _get(request, pk)
    a = get_object_or_404(Attachment, pk=att_id, work_order=wo)
    return FileResponse(a.file.open("rb"), as_attachment=request.GET.get("download") == "1", filename=a.filename)


@login_required
def wo_pdf(request, pk):
    wo = _get(request, pk)
    if wo.number and wo.pdf and request.GET.get("fresh") != "1":
        try:
            return FileResponse(wo.pdf.open("rb"), filename=f"{wo.number}.pdf", content_type="application/pdf")
        except FileNotFoundError:
            pass
    from .pdf import render_pdf
    data = render_pdf(wo)
    resp = HttpResponse(data, content_type="application/pdf")
    name = wo.number or f"DRAFT-WO-{wo.pid}-{wo.team.code}"
    resp["Content-Disposition"] = f'inline; filename="{name}.pdf"'
    return resp


@login_required
def team_access(request):
    if not services.is_admin(request.user):
        return _forbid("Only Admins and Super Admins can manage access.")
    if request.method == "POST":
        target = get_object_or_404(User, pk=request.POST.get("user_id"))
        try:
            if services.set_access(target, request.POST.get("level", ""), request.user):
                messages.success(request, f"Access updated for {target.full_name or target.email}.")
        except services.WorkOrderError as e:
            messages.error(request, str(e))
        return redirect(reverse("work_orders:access") + (f"?q={request.POST.get('q')}" if request.POST.get("q") else ""))
    q = request.GET.get("q", "").strip()
    users = User.objects.filter(is_active=True).order_by("full_name", "email")
    if q:
        users = users.filter(Q(full_name__icontains=q) | Q(email__icontains=q))
    access = {a.user_id: a for a in WorkOrderAccess.objects.select_related("granted_by")}
    rows = []
    for u in users:
        admin = services.is_admin(u)
        a = access.get(u.id)
        rows.append({"user": u, "admin": admin, "approver": services.can_approve(u),
                     "level": services.LEVEL_ADMIN if admin else (a.level if a else ""),
                     "granted_by": a.granted_by if a else None, "updated_at": a.updated_at if a else None})
    rows.sort(key=lambda r: (0 if r["admin"] else 1 if r["level"] else 2, (r["user"].full_name or r["user"].email).lower()))
    return render(request, "work_orders/access.html", _ctx(
        request, rows=rows, q=q, changes=WorkOrderAccessChange.objects.select_related("user", "changed_by")[:30]))
