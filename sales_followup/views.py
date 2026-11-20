import logging
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Max, Q, Value, When
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .models import FollowupAccess, FollowupAccessChange, FollowupEntry, SalesFollowup

logger = logging.getLogger(__name__)
User = get_user_model()
NO_ACCESS = "You don't have access to Sales Follow-up. Please ask an admin."
SALES_EMAILS = ["sales@flow-force.com", "sales@flowforceengineering.com"]


def _forbid(msg=NO_ACCESS):
    return HttpResponseForbidden(msg)


def _ctx(request, **extra):
    extra.update({"sf_is_manager": services.is_manager(request.user), "sf_is_admin": services.is_admin(request.user),
                  "statuses": SalesFollowup.STATUS_CHOICES, "entry_statuses": SalesFollowup.ENTRY_STATUSES,
                  "channels": FollowupEntry.CHANNEL_CHOICES, "today": timezone.localdate()})
    return extra



def _contacts():
    seen = FollowupEntry.objects.values_list("contacted", flat=True).distinct()[:200]
    return sorted(set(seen) | set(SALES_EMAILS), key=str.lower)


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def _entry_data(post):
    return {"date": _parse_date(post.get("date")) or timezone.localdate(), "contacted": post.get("contacted", ""),
            "channel": post.get("channel", "EMAIL"), "update_text": post.get("update_text", ""),
            "status": post.get("status", ""), "next_date": _parse_date(post.get("next_date"))}


@login_required
def followup_list(request):
    if not services.can_use(request.user):
        return _forbid()
    try:
        services.sync_all()
    except Exception as e:
        logger.exception("sales follow-up sync failed: %s", e)
    today = timezone.localdate()
    q = request.GET.get("q", "").strip()
    view = request.GET.get("view", "open")
    base = SalesFollowup.objects.all()
    if q:
        base = base.filter(Q(enquiry_no__icontains=q) | Q(customer_name__icontains=q) | Q(zoho_number__icontains=q)
                           | Q(vendor_name__icontains=q))
    open_q = ~Q(status__in=SalesFollowup.CLOSED)
    due_q = open_q & Q(next_followup_date__lte=today)
    cards = [
        ("open", "Open", base.filter(open_q).count(), "text-slate-900"),
        ("due", "Due today / overdue", base.filter(due_q).count(), "text-red-600"),
        ("never", "No follow-up yet", base.filter(open_q, followup_count=0).count(), "text-amber-600"),
        ("WON", "PO received / Won", base.filter(status=SalesFollowup.STATUS_WON).count(), "text-emerald-600"),
        ("LOST", "Lost", base.filter(status=SalesFollowup.STATUS_LOST).count(), "text-slate-500"),
        ("all", "All approved", base.count(), "text-blue-700"),
    ]
    qs = base
    if view == "open":
        qs = qs.filter(open_q)
    elif view == "due":
        qs = qs.filter(due_q)
    elif view == "never":
        qs = qs.filter(open_q, followup_count=0)
    elif view in dict(SalesFollowup.STATUS_CHOICES):
        qs = qs.filter(status=view)
    qs = qs.annotate(closed=Case(When(status__in=SalesFollowup.CLOSED, then=Value(1)), default=Value(0),
                                 output_field=IntegerField())).order_by("closed", "next_followup_date", "-approved_at")
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "sales_followup/list.html", _ctx(request, page=page, q=q, view=view, cards=cards))


@login_required
def followup_detail(request, pk):
    if not services.can_use(request.user):
        return _forbid()
    fu = get_object_or_404(SalesFollowup, pk=pk)
    if request.method == "POST":
        try:
            services.add_entry(fu, _entry_data(request.POST), request.user)
        except services.FollowupError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Follow-up recorded.")
            return redirect("sales_followup:detail", fu.pk)
    entries = fu.entries.select_related("created_by")
    return render(request, "sales_followup/detail.html", _ctx(
        request, fu=fu, entries=entries, contacts=_contacts(),
        can_change={e.id: services.can_change_entry(request.user, e) for e in entries},
        default_next=timezone.localdate() + timedelta(days=1),
        logs=fu.logs.select_related("user")[:30]))



@login_required
def entry_edit(request, entry_id):
    if not services.can_use(request.user):
        return _forbid()
    entry = get_object_or_404(FollowupEntry.objects.select_related("followup"), pk=entry_id)
    if not services.can_change_entry(request.user, entry):
        return _forbid("You can only change your own entries.")
    if request.method == "POST":
        try:
            services.edit_entry(entry, _entry_data(request.POST), request.user)
        except services.FollowupError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Entry updated.")
            return redirect("sales_followup:detail", entry.followup_id)
    return render(request, "sales_followup/entry_edit.html", _ctx(request, entry=entry, fu=entry.followup,
                                                                   contacts=_contacts()))


@login_required
@require_POST
def entry_delete(request, entry_id):
    if not services.can_use(request.user):
        return _forbid()
    entry = get_object_or_404(FollowupEntry, pk=entry_id)
    fu_id = entry.followup_id
    try:
        services.delete_entry(entry, request.user)
    except services.FollowupError as e:
        return _forbid(str(e))
    messages.success(request, "Entry deleted.")
    return redirect("sales_followup:detail", fu_id)


@login_required
def monitor(request):
    if not services.is_manager(request.user):
        return _forbid("Only managers can see the monitoring page.")
    services.sync_all()
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    open_q = ~Q(status__in=SalesFollowup.CLOSED)
    by_user = (FollowupEntry.objects.values("created_by__full_name", "created_by__email")
               .annotate(today=Count("id", filter=Q(date=today)),
                         week=Count("id", filter=Q(date__gte=week_start)),
                         month=Count("id", filter=Q(date__gte=month_start)),
                         total=Count("id"), last=Max("created_at"))
               .order_by("-week"))
    totals = {
        "open": SalesFollowup.objects.filter(open_q).count(),
        "overdue": SalesFollowup.objects.filter(open_q, next_followup_date__lt=today).count(),
        "due_today": SalesFollowup.objects.filter(open_q, next_followup_date=today).count(),
        "never": SalesFollowup.objects.filter(open_q, followup_count=0).count(),
        "won": SalesFollowup.objects.filter(status=SalesFollowup.STATUS_WON).count(),
        "lost": SalesFollowup.objects.filter(status=SalesFollowup.STATUS_LOST).count(),
        "week_entries": FollowupEntry.objects.filter(date__gte=week_start).count(),
    }
    recent = FollowupEntry.objects.select_related("followup", "created_by")[:25]
    return render(request, "sales_followup/monitor.html", _ctx(
        request, by_user=by_user, totals=totals, recent=recent))


@login_required
def team_access(request):
    if not services.is_admin(request.user):
        return _forbid("Only Admins and Super Admins can manage access.")
    if request.method == "POST":
        target = get_object_or_404(User, pk=request.POST.get("user_id"))
        try:
            if services.set_access(target, request.POST.get("level", ""), request.user):
                messages.success(request, f"Access updated for {target.full_name or target.email}.")
        except services.FollowupError as e:
            messages.error(request, str(e))
        return redirect(reverse("sales_followup:access") + (f"?q={request.POST.get('q')}" if request.POST.get("q") else ""))
    q = request.GET.get("q", "").strip()
    users = User.objects.filter(is_active=True).order_by("full_name", "email")
    if q:
        users = users.filter(Q(full_name__icontains=q) | Q(email__icontains=q))
    access = {a.user_id: a for a in FollowupAccess.objects.select_related("granted_by")}
    rows = []
    for u in users:
        admin = services.is_admin(u)
        a = access.get(u.id)
        rows.append({"user": u, "admin": admin, "level": services.LEVEL_ADMIN if admin else (a.level if a else ""),
                     "granted_by": a.granted_by if a else None, "updated_at": a.updated_at if a else None})
    rows.sort(key=lambda r: (0 if r["admin"] else 1 if r["level"] else 2, (r["user"].full_name or r["user"].email).lower()))
    return render(request, "sales_followup/access.html", _ctx(
        request, rows=rows, q=q, changes=FollowupAccessChange.objects.select_related("user", "changed_by")[:30]))
