from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .forms import EnquiryForm
from .models import AccessChange, Remark, TrackerAccess, VendorEnquiry

User = get_user_model()
NO_ACCESS = "You don't have access to the Vendor Enquiry Tracker. Please ask an admin."


def _forbid(msg=NO_ACCESS):
    return HttpResponseForbidden(msg)


def _get(request, pk):
    """The row, only if this user may see it (404 otherwise, so ids don't leak)."""
    if not services.can_use(request.user):
        return None, _forbid()
    enq = services.visible_qs(request.user).filter(pk=pk).first()
    if not enq:
        raise Http404
    return enq, None


def _ctx(request, **extra):
    extra.update({
        "ve_level": services.level(request.user),
        "ve_is_manager": services.is_manager(request.user),
        "ve_is_admin": services.is_admin(request.user),
        "statuses": VendorEnquiry.STATUS_CHOICES,
    })
    return extra


@login_required
def enquiry_list(request):
    if not services.can_use(request.user):
        return _forbid()
    base = services.visible_qs(request.user)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    owner = request.GET.get("owner", "")
    qs = base
    if q:
        qs = qs.filter(Q(enquiry_no__icontains=q) | Q(customer_name__icontains=q) | Q(vendor_name__icontains=q)
                       | Q(vendor_quote_no__icontains=q) | Q(initial_enquiry__icontains=q))
    if owner and services.is_manager(request.user):
        qs = qs.filter(created_by_id=owner)
    counts_qs = qs
    if status in dict(VendorEnquiry.STATUS_CHOICES):
        qs = qs.filter(status=status)
    counts = {row["status"]: row["n"] for row in counts_qs.values("status").annotate(n=Count("id"))}
    cards = [(key, label, counts.get(key, 0)) for key, label in VendorEnquiry.STATUS_CHOICES]
    qs = qs.annotate(n_files=Count("files", distinct=True), n_remarks=Count("remarks", filter=Q(remarks__parent=None), distinct=True))
    page = Paginator(qs.order_by("-updated_at"), 50).get_page(request.GET.get("page"))
    owners = []
    if services.is_manager(request.user):
        owners = User.objects.filter(vendor_enquiries__isnull=False).distinct().order_by("full_name")
    return render(request, "vendor_enquiries/list.html", _ctx(
        request, page=page, q=q, status=status, owner=owner, owners=owners, cards=cards,
        total=sum(counts.values())))


def _form_initial(enq=None):
    if enq is None:
        return {"sent_on": timezone.localdate(), "status": VendorEnquiry.STATUS_SENT}
    return {"company": enq.company, "enquiry_no": enq.enquiry_no, "customer_name": enq.customer_name, "vendor_name": enq.vendor_name,
            "initial_enquiry": enq.initial_enquiry, "sent_on": enq.sent_on, "status": enq.status,
            "vendor_quote_no": enq.vendor_quote_no}


def _save_form(request, enq, template):
    form = EnquiryForm(request.POST or None, request.FILES or None, initial=_form_initial(enq if enq.pk else None))
    if request.method == "POST" and form.is_valid():
        try:
            enq, warnings = services.save_enquiry(enq, form.cleaned_data, request.user,
                                                  upload=form.cleaned_data.get("quote_file"))
        except services.TrackerError as e:
            form.add_error(None, str(e))
        else:
            for w in warnings:
                messages.warning(request, w)
            messages.success(request, "Saved." + (" The vendor quote was emailed to GM sir." if request.FILES.get("quote_file") else ""))
            return redirect("vendor_enquiries:detail", enq.pk)
    return render(request, template, _ctx(request, form=form, enq=enq if enq.pk else None,
                                          vendor_names=services.vendor_names(),
                                          has_file=bool(enq.pk and enq.files.exists())))


@login_required
def enquiry_create(request):
    if not services.can_use(request.user):
        return _forbid()
    return _save_form(request, VendorEnquiry(), "vendor_enquiries/form.html")


@login_required
def enquiry_edit(request, pk):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    if not services.can_edit(request.user, enq):
        return _forbid("Only the person who added this enquiry can edit it.")
    return _save_form(request, enq, "vendor_enquiries/form.html")


@login_required
def enquiry_detail(request, pk):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    remarks = (enq.remarks.filter(parent=None).select_related("author")
               .prefetch_related(Prefetch("replies", queryset=Remark.objects.select_related("author"))))
    from vendor_quotes.services import workflow
    return render(request, "vendor_enquiries/detail.html", _ctx(
        request, enq=enq, files=enq.files.select_related("uploaded_by"), remarks=remarks,
        logs=enq.logs.select_related("user")[:50],
        can_edit=services.can_edit(request.user, enq), can_remark=services.can_remark(request.user, enq),
        can_reply=services.can_reply(request.user, enq),
        can_send=services.can_send_to_pricing(request.user, enq),
        can_open_quote=workflow.can_edit(request.user),
        latest=enq.latest_file))


@login_required
def enquiry_file(request, pk, file_id):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    qf = get_object_or_404(enq.files, pk=file_id)
    return FileResponse(qf.file.open("rb"), as_attachment=request.GET.get("download") == "1", filename=qf.filename)


@login_required
@require_POST
def enquiry_delete(request, pk):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    if not services.can_edit(request.user, enq):
        return _forbid("Only the person who added this enquiry can delete it.")
    label = enq.label
    services.delete(enq, request.user)
    messages.success(request, f"Deleted {label}.")
    return redirect("vendor_enquiries:list")


@login_required
@require_POST
def enquiry_revise(request, pk):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    if not services.can_edit(request.user, enq):
        return _forbid("Only the person who added this enquiry can upload a revision.")
    try:
        qf = services.revise(enq, request.FILES.get("quote_file"), request.user, request.POST.get("vendor_quote_no", ""))
    except services.TrackerError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{qf.label} uploaded and emailed to GM sir.")
    return redirect("vendor_enquiries:detail", enq.pk)


@login_required
@require_POST
def remark_add(request, pk):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    if not services.can_remark(request.user, enq):
        return _forbid("Only managers can add remarks.")
    try:
        services.add_remark(enq, request.POST.get("text", ""), request.user)
    except services.TrackerError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Remark added and emailed.")
    return redirect(reverse("vendor_enquiries:detail", args=[enq.pk]) + "#remarks")


@login_required
@require_POST
def remark_reply(request, pk, remark_id):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    if not services.can_reply(request.user, enq):
        return _forbid()
    remark = get_object_or_404(Remark, pk=remark_id, enquiry=enq, parent=None)
    try:
        services.add_reply(enq, remark, request.POST.get("text", ""), request.user)
    except services.TrackerError as e:
        messages.error(request, str(e))
    return redirect(reverse("vendor_enquiries:detail", args=[enq.pk]) + f"#remark-{remark.pk}")


@login_required
def send_to_pricing(request, pk):
    enq, resp = _get(request, pk)
    if resp:
        return resp
    if not services.can_send_to_pricing(request.user, enq):
        return _forbid("Only managers with Vendor Quotes access can send to Price Calculation.")
    if request.method != "POST":
        return redirect("vendor_enquiries:detail", enq.pk)
    try:
        quote, msg = services.send_to_pricing(enq, request.user, create_vendor=request.POST.get("create_vendor") == "1")
    except services.TrackerError as e:
        if str(e) == "VENDOR_MISSING":
            return render(request, "vendor_enquiries/confirm_vendor.html", _ctx(request, enq=enq))
        messages.error(request, str(e))
        return redirect("vendor_enquiries:detail", enq.pk)
    if request.POST.get("create_vendor") == "1":
        msg += f' Vendor "{quote.vendor.name}" was created with name only - please set its currency and margins in Vendors.'
    messages.success(request, msg)
    return redirect("vendor_quotes:quote_detail", quote.pk)


@login_required
def team_access(request):
    if not services.is_admin(request.user):
        return _forbid("Only Admins and Super Admins can manage access.")
    if request.method == "POST":
        target = get_object_or_404(User, pk=request.POST.get("user_id"))
        try:
            changed = services.set_access(target, request.POST.get("level", ""), request.user)
        except services.TrackerError as e:
            messages.error(request, str(e))
        else:
            if changed:
                messages.success(request, f"Access updated for {target.full_name or target.email}.")
        return redirect(reverse("vendor_enquiries:access") + (f"?q={request.POST.get('q')}" if request.POST.get("q") else ""))
    q = request.GET.get("q", "").strip()
    users = User.objects.filter(is_active=True).order_by("full_name", "email")
    if q:
        users = users.filter(Q(full_name__icontains=q) | Q(email__icontains=q))
    access = {a.user_id: a for a in TrackerAccess.objects.select_related("granted_by")}
    rows = []
    for u in users:
        admin = services.is_admin(u)
        a = access.get(u.id)
        rows.append({"user": u, "admin": admin, "level": services.LEVEL_ADMIN if admin else (a.level if a else ""),
                     "granted_by": a.granted_by if a else None, "updated_at": a.updated_at if a else None})
    rows.sort(key=lambda r: (0 if r["admin"] else 1 if r["level"] else 2, (r["user"].full_name or r["user"].email).lower()))
    changes = AccessChange.objects.select_related("user", "changed_by")[:30]
    return render(request, "vendor_enquiries/access.html", _ctx(
        request, rows=rows, q=q, changes=changes, levels=TrackerAccess.LEVEL_CHOICES))
