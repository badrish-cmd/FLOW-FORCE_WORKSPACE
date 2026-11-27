"""Views for the single (landscape) work order."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import pid as pid_svc
from . import project_defaults as D
from . import project_services as ps
from . import services
from .models import Team, WOItem, WorkOrder
from .views import _ctx, _get


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


def _header_post(P):
    data = {k: P.get(k, "") for k in ps.HEADER_TEXT}
    data["order_date"] = _date(P.get("order_date"))
    data["required_by"] = _date(P.get("required_by"))
    data["project_manager"] = P.get("project_manager", "")
    data["wo_type"] = P.get("wo_type", "")
    data["reference"] = P.get("reference", "")
    lines = [{"description": desc, "qty": _dec(qty), "unit": unit, "row_id": int(row) if row.isdigit() else None}
             for desc, qty, unit, row in zip(P.getlist("line_desc"), P.getlist("line_qty"), P.getlist("line_unit"),
                                             P.getlist("line_row"))]
    return data, lines


def _form(request, wo, p):
    return render(request, "work_orders/project_form.html", _ctx(
        request, wo=wo, p=p, types=D.TYPES, employees=services.employees(), recent_pids=pid_svc.recent_pids()))


@login_required
def create(request):
    if not services.can_create(request.user):
        return HttpResponseForbidden("You don't have permission to create work orders.")
    if request.method == "POST":
        data, lines = _header_post(request.POST)
        internal = request.POST.get("internal") == "1"
        pid = request.POST.get("pid", "").strip()
        head = None if internal else pid_svc.load_pid(pid)
        try:
            for f in request.FILES.getlist("files"):
                services.validate_file(f)
            wo = ps.create(request.user, wo_type=data["wo_type"], pid=pid, is_internal=internal,
                           reference=data["reference"], data=data, lines=lines, pid_head=head)
            services._add_files(wo, request.FILES.getlist("files"), request.user)
        except services.WorkOrderError as e:
            messages.error(request, str(e))
            p = dict(data, pid=pid, internal=internal, lines=lines, step=2)
            return _form(request, None, p)
        messages.success(request, "Draft created with the default stages, departments and checklist for "
                                  f"{wo.type_label}. Add or remove what this job needs, then send it to the GM.")
        return redirect("work_orders:detail", wo.pk)
    pid = request.GET.get("pid", "").strip()
    wo_type = request.GET.get("type", D.TYPE_PROJECT)
    if request.GET.get("internal") == "1":
        p = {"step": 2, "internal": True, "reference": request.GET.get("ref", "").strip(), "pid": "INT",
             "customer_name": "PT. Flow Force Indonesia", "wo_type": wo_type, "lines": [{"description": ""}]}
        return _form(request, None, p)
    if pid:
        head = pid_svc.load_pid(pid)
        if head:
            head.update({"step": 2, "wo_type": wo_type, "project_manager": str(request.user.id)})
            if (head.get("kind") or "").upper().startswith("SUPPL") and "type" not in request.GET:
                head["wo_type"] = D.TYPE_SUPPLY
            return _form(request, None, head)
        messages.error(request, f"PID {pid} not found in LIST PID - PT FLOW FORCE INDONESIA.")
    return _form(request, None, {"step": 1, "wo_type": wo_type})


@login_required
def edit(request, pk):
    wo = _get(request, pk)
    if not ps.can_edit(request.user, wo):
        messages.error(request, "This can be changed only while the work order is a draft. After approval use Revise.")
        return redirect("work_orders:detail", wo.pk)
    if request.method == "POST":
        data, lines = _header_post(request.POST)
        try:
            for f in request.FILES.getlist("files"):
                services.validate_file(f)
            ps.update_header(wo, request.user, data, lines)
            services._add_files(wo, request.FILES.getlist("files"), request.user)
            messages.success(request, "Saved.")
        except services.WorkOrderError as e:
            messages.error(request, str(e))
        return redirect("work_orders:detail", wo.pk)
    p = {f: getattr(wo, f) for f in ps.HEADER_TEXT}
    p.update({"step": 2, "pid": wo.pid, "internal": wo.is_internal, "reference": wo.reference, "wo_type": wo.wo_type,
              "order_date": wo.order_date, "required_by": wo.required_by,
              "project_manager": str(wo.project_manager_id or ""),
              "lines": [{"description": l.description, "qty": l.qty, "unit": l.unit, "row_id": l.source_row_id}
                        for l in wo.lines.all()]})
    return _form(request, wo, p)


def detail(request, wo):
    u = request.user
    depts = list(wo.departments.select_related("team", "pic").prefetch_related("members", "items__assigned_to",
                                                                                 "items__done_by", "items__attachment"))
    for dp in depts:
        dp.label = ps.dept_label(dp.team)
        dp.can_add = ps.can_add_item(u, wo, dp)
        dp.all_items = list(dp.items.all())
        for it in dp.all_items:
            it.department = dp  # avoid queries
            it.can_tick = ps.can_tick(u, it)
    stages = list(wo.wo_stages.select_related("done_by"))
    for st in stages:
        st.work_order = wo
        st.can_mark = ps.can_mark_stage(u, st)
    used = {d.team_id for d in depts}
    editing = ps.can_edit(u, wo)
    return render(request, "work_orders/project_detail.html", _ctx(
        request, wo=wo, depts=depts, stages=stages, lines=wo.lines.all(), prog=ps.progress(wo),
        spec_rows=wo.spec_rows.all(), ref_docs=wo.ref_docs.all(), editing=editing,
        free_teams=Team.objects.filter(is_active=True).exclude(pk__in=used), all_teams=Team.objects.filter(is_active=True),
        employees=services.employees(), dept_labels=ps.DEPT_LABELS,
        attachments=wo.attachments.select_related("uploaded_by"), notes_list=wo.progress_notes.select_related("author"),
        events=wo.events.select_related("user")[:80], revisions=wo.revisions.all(),
        can_submit=editing, can_delete=services.can_delete(u, wo),
        can_decide=wo.status == WorkOrder.STATUS_IN_REVIEW and services.can_approve(u),
        can_work=services.can_work(u, wo), can_close=services.can_close(u, wo), is_admin=services.is_admin(u),
        can_spec=ps.can_edit_spec(u, wo), can_confirm=ps.can_confirm_spec(u, wo),
        can_cancel=wo.status in WorkOrder.ACTIVE + (WorkOrder.STATUS_DONE,) and (services.can_approve(u) or services.is_admin(u)),
        can_revise=wo.status in WorkOrder.ACTIVE + (WorkOrder.STATUS_DONE,) and (services.can_approve(u) or services.can_create(u)),
        is_project=True))


def _go(request, wo, fn, ok=None, anchor=""):
    try:
        fn()
        if ok:
            messages.success(request, ok)
    except services.WorkOrderError as e:
        messages.error(request, str(e))
    except (WOItem.DoesNotExist, ValueError):
        messages.error(request, "That item no longer exists.")
    except Exception as e:  # DoesNotExist of stage / department
        if e.__class__.__name__ != "DoesNotExist":
            raise
        messages.error(request, "That item no longer exists.")
    return redirect(reverse("work_orders:detail", args=[wo.pk]) + (f"#{anchor}" if anchor else ""))


# ---------------------------------------------------------------- stages

@login_required
@require_POST
def stage_add(request, pk):
    wo = _get(request, pk)
    P = request.POST
    return _go(request, wo, lambda: ps.add_stage(wo, request.user, {"name": P.get("name"), "description": P.get("description"),
                                                                     "teams": P.getlist("teams"),
                                                                     "target_date": _date(P.get("target_date"))}),
               anchor="workflow")


@login_required
@require_POST
def stage_update(request, pk, stage_id):
    wo = _get(request, pk)
    P = request.POST
    if P.get("delete") == "1":
        return _go(request, wo, lambda: ps.delete_stage(wo, request.user, stage_id), anchor="workflow")
    return _go(request, wo, lambda: ps.update_stage(wo, request.user, stage_id, {
        "name": P.get("name"), "description": P.get("description"), "teams": P.getlist("teams"),
        "target_date": _date(P.get("target_date"))}), anchor="workflow")


@login_required
@require_POST
def stage_done(request, pk, stage_id):
    wo = _get(request, pk)
    if request.POST.get("reopen") == "1":
        return _go(request, wo, lambda: ps.reopen_stage(wo, request.user, stage_id), anchor="workflow")
    return _go(request, wo, lambda: ps.mark_stage(wo, request.user, stage_id), anchor="workflow")


# ---------------------------------------------------------------- departments

@login_required
@require_POST
def dept_add(request, pk):
    wo = _get(request, pk)
    return _go(request, wo, lambda: ps.add_department(wo, request.user, request.POST.get("team_id")), anchor="departments")


@login_required
@require_POST
def dept_update(request, pk, dept_id):
    wo = _get(request, pk)
    P = request.POST
    if P.get("delete") == "1":
        return _go(request, wo, lambda: ps.delete_department(wo, request.user, dept_id), anchor="departments")
    return _go(request, wo, lambda: ps.update_department(wo, request.user, dept_id, pic_id=P.get("pic"),
                                                         member_ids=P.getlist("members"),
                                                         target_date=_date(P.get("target_date"))),
               anchor=f"dept-{dept_id}")


# ---------------------------------------------------------------- items

@login_required
@require_POST
def item_add(request, pk, dept_id):
    wo = _get(request, pk)
    P = request.POST
    return _go(request, wo, lambda: ps.add_item(wo, request.user, dept_id, kind=P.get("kind"), text=P.get("text"),
                                                assigned_to_id=P.get("assigned_to"), file_required=P.get("file_required") == "1",
                                                target_date=_date(P.get("target_date"))), anchor=f"dept-{dept_id}")


@login_required
@require_POST
def item_update(request, pk, item_id):
    wo = _get(request, pk)
    P = request.POST
    anchor = f"dept-{P.get('dept', '')}"
    if P.get("delete") == "1":
        return _go(request, wo, lambda: ps.delete_item(wo, request.user, item_id), anchor=anchor)
    return _go(request, wo, lambda: ps.update_item(wo, request.user, item_id, text=P.get("text"),
                                                   assigned_to_id=P.get("assigned_to"),
                                                   file_required=P.get("file_required") == "1",
                                                   target_date=_date(P.get("target_date"))), anchor=anchor)


@login_required
@require_POST
def item_tick(request, pk, item_id):
    wo = _get(request, pk)
    P = request.POST
    anchor = f"dept-{P.get('dept', '')}"
    nxt = P.get("next")
    action = P.get("action", "done")
    if action == "na":
        fn = lambda: ps.na_item(wo, request.user, item_id, P.get("reason"))
    elif action == "reopen":
        fn = lambda: ps.reopen_item(wo, request.user, item_id)
    else:
        fn = lambda: ps.tick_item(wo, request.user, item_id, doc_ref=P.get("doc_ref", ""), file=request.FILES.get("file"))
    resp = _go(request, wo, fn, anchor=anchor)
    if nxt == "my":
        return redirect("work_orders:my_tasks")
    return resp


# ---------------------------------------------------------------- page 2

@login_required
@require_POST
def spec_save(request, pk):
    wo = _get(request, pk)
    P = request.POST
    changed = set(P.getlist("spec_changed"))
    rows = [{"parameter": a, "requirement": b, "final": c, "reason": r, "changed": str(i) in changed}
            for i, (a, b, c, r) in enumerate(zip(P.getlist("spec_parameter"), P.getlist("spec_requirement"),
                                                 P.getlist("spec_final"), P.getlist("spec_reason")))]
    docs = [{"document": a, "reference": b, "revision": c, "doc_date": _date(dt), "source": s}
            for a, b, c, dt, s in zip(P.getlist("doc_document"), P.getlist("doc_reference"), P.getlist("doc_revision"),
                                      P.getlist("doc_date"), P.getlist("doc_source"))]
    return _go(request, wo, lambda: ps.save_spec(wo, request.user, rows, docs), "Specification saved.", anchor="spec")


@login_required
@require_POST
def spec_confirm(request, pk):
    wo = _get(request, pk)
    P = request.POST
    return _go(request, wo, lambda: ps.confirm_spec(wo, request.user, customer_by=P.get("customer_by"),
                                                    customer_date=_date(P.get("customer_date")), method=P.get("method")),
               "Specification confirmed.", anchor="spec")


# ---------------------------------------------------------------- my tasks

@login_required
def my_tasks(request):
    items = list(ps.my_open_items(request.user))
    for it in items:
        it.can_tick = ps.can_tick(request.user, it)
        it.dept_label = ps.dept_label(it.department.team)
    stages = ps.my_open_stages(request.user)
    for st in stages:
        st.can_mark = ps.can_mark_stage(request.user, st)
    groups = {}
    for it in items:
        groups.setdefault(it.department.work_order_id, {"wo": it.department.work_order, "items": [], "stages": []})["items"].append(it)
    for st in stages:
        groups.setdefault(st.work_order_id, {"wo": st.work_order, "items": [], "stages": []})["stages"].append(st)
    return render(request, "work_orders/my_tasks.html", _ctx(request, groups=list(groups.values()), nav="tasks",
                                                             total=len(items) + len(stages)))
