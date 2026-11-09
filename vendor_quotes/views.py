import csv
import json
import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Prefetch, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import MasterUploadForm, QuoteCreateForm, VendorForm
from .models import (AIRequestLog, Enquiry, MasterSheet, QuoteRevision, Vendor, VendorMasterFile, VendorQuote,
                     ZohoConnection)
from .services import workflow, zoho
from .services.sheet import extract_lines


User = get_user_model()


def _guard(request, approve=False):
    ok = workflow.can_approve(request.user) if approve else workflow.can_edit(request.user)
    return None if ok else HttpResponseForbidden("You don't have access to Vendor Quotes.")


def _ctx(request, **extra):
    is_admin = (
        workflow.can_approve(request.user)
        or getattr(request.user, "role", None) in ("SUPER_ADMIN", "ADMIN")
        or getattr(request.user, "is_superuser", False)
    )
    has_gemini_key = bool(
        getattr(settings, "GEMINI_API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    )
    extra.setdefault("can_approve", workflow.can_approve(request.user))
    extra.setdefault("can_edit", workflow.can_edit(request.user))
    extra.setdefault("is_admin", is_admin)
    extra.setdefault("has_gemini_key", has_gemini_key)
    return extra


# ---------------------------------------------------------------- lists

@login_required
def quote_list(request):
    if (resp := _guard(request)):
        return resp
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status") or ""
    latest = QuoteRevision.objects.order_by("-number")
    quotes = (VendorQuote.objects.select_related("enquiry", "vendor", "created_by")
              .prefetch_related(Prefetch("revisions", queryset=latest)))
    if q:
        quotes = quotes.filter(Q(enquiry__number__icontains=q) | Q(enquiry__customer_name__icontains=q)
                               | Q(vendor__name__icontains=q) | Q(vendor_quote_no__icontains=q)
                               | Q(revisions__zoho_estimate_number__icontains=q)).distinct()
    rows = []
    for quote in quotes[:500]:
        revs = list(quote.revisions.all())
        cur = revs[0] if revs else None
        if status and (not cur or cur.status != status):
            continue
        rows.append({"quote": quote, "rev": cur})
    waiting = [r for r in rows if r["rev"] and r["rev"].status == QuoteRevision.STATUS_IN_REVIEW]
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    counts = dict(QuoteRevision.objects.values_list("status").annotate(n=Count("id")))
    return render(request, "vendor_quotes/quote_list.html", _ctx(
        request, page=page, waiting=waiting, q=q, status=status, statuses=QuoteRevision.STATUS_CHOICES,
        counts=counts, zoho_connected=ZohoConnection.get().is_connected))


@login_required
def enquiry_detail(request, pk):
    if (resp := _guard(request)):
        return resp
    enquiry = get_object_or_404(Enquiry, pk=pk)
    latest = QuoteRevision.objects.order_by("-number")
    quotes = enquiry.quotes.select_related("vendor").prefetch_related(Prefetch("revisions", queryset=latest))
    if request.method == "POST":
        enquiry.customer_name = (request.POST.get("customer_name") or enquiry.customer_name).strip()
        enquiry.zoho_customer_id = (request.POST.get("zoho_customer_id") or "").strip()
        enquiry.notes = request.POST.get("notes", enquiry.notes)
        enquiry.save()
        messages.success(request, "Enquiry updated.")
        return redirect("vendor_quotes:enquiry_detail", pk=enquiry.pk)
    can_delete_enquiry = workflow.can_delete_enquiry(enquiry, request.user)
    has_locked_quotes = QuoteRevision.objects.filter(
        quote__enquiry=enquiry,
        status__in=[QuoteRevision.STATUS_APPROVED, QuoteRevision.STATUS_VOID]
    ).exists()
    return render(request, "vendor_quotes/enquiry_detail.html", _ctx(
        request, enquiry=enquiry, quotes=quotes, zoho_connected=ZohoConnection.get().is_connected,
        can_delete_enquiry=can_delete_enquiry, has_locked_quotes=has_locked_quotes))


# ---------------------------------------------------------------- create

@login_required
def quote_create(request):
    if (resp := _guard(request)):
        return resp
    form = QuoteCreateForm(request.POST or None, request.FILES or None,
                           initial={"enquiry_number": request.GET.get("enquiry", ""),
                                    "customer_name": request.GET.get("customer", "")})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        start_from = d["previous_sheet"].snapshot if d["start"] == form.START_PREVIOUS else None
        quote, _rev = workflow.create_quote(
            enquiry_number=d["enquiry_number"], customer_name=d["customer_name"], vendor=d["vendor"],
            vendor_quote_no=d.get("vendor_quote_no") or "", quote_file=d.get("quote_file"),
            user=request.user, start_from=start_from)
        messages.success(request, "Vendor quote added. Fill in the price table, save, then send it for approval.")
        return redirect("vendor_quotes:quote_detail", pk=quote.pk)
    enquiries = Enquiry.objects.order_by("-created_at").values("number", "customer_name")[:300]
    sheets = (MasterSheet.objects.select_related("vendor").only("id", "vendor_id", "vendor__name", "vendor__code",
              "sheet_name", "enquiry_no", "customer_name", "position").order_by("vendor__name", "-position"))
    return render(request, "vendor_quotes/quote_create.html", _ctx(
        request, form=form, enquiries=list(enquiries), sheets=sheets,
        has_vendors=Vendor.objects.filter(is_active=True).exists()))


# ---------------------------------------------------------------- quote page (sheet editor + review)

@login_required
def quote_detail(request, pk):
    if (resp := _guard(request)):
        return resp
    quote = get_object_or_404(VendorQuote.objects.select_related("enquiry", "vendor", "created_by"), pk=pk)
    revisions = list(quote.revisions.select_related("created_by", "decided_by").order_by("number"))
    if not revisions:
        raise Http404
    wanted = request.GET.get("rev")
    rev = next((r for r in revisions if str(r.number) == wanted), revisions[-1])
    lines, problems = extract_lines(rev.snapshot)
    total = sum(ln["qty"] * ln["rate"] for ln in lines)
    editable = workflow.can_edit_revision(request.user, rev)
    ai_res = quote.ai_result or {}
    ai_lines_count = len(ai_res.get("lines") or [])
    ai_warnings = ai_res.get("warnings") or []
    ai_quote_total = ai_res.get("quote_total")

    can_delete = workflow.can_delete_quote(quote, request.user)
    has_approved_or_void = quote.revisions.filter(status__in=[QuoteRevision.STATUS_APPROVED, QuoteRevision.STATUS_VOID]).exists()
    can_discard = workflow.can_discard_revision(rev, request.user)
    can_void = workflow.can_void_revision(rev, request.user)
    last_approved_rev = quote.revisions.filter(status=QuoteRevision.STATUS_APPROVED).order_by("-number").first()
    has_open_revision = revisions[-1].status in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_IN_REVIEW, QuoteRevision.STATUS_SENT_BACK)
    can_revise = workflow.can_edit(request.user) and (last_approved_rev is not None and rev.id == last_approved_rev.id) and not has_open_revision
    next_revision_number = (revisions[-1].number + 1) if revisions else 1

    ai_logs = quote.ai_request_logs.all()
    quote_ai_count = ai_logs.count()
    quote_ai_tokens = sum(l.total_tokens for l in ai_logs)
    quote_ai_cost = sum((l.estimated_cost_usd for l in ai_logs), Decimal("0"))
    is_budget_blocked = workflow.is_ai_budget_blocked()

    return render(request, "vendor_quotes/quote_detail.html", _ctx(
        request, quote=quote, rev=rev, revisions=revisions, editable=editable,
        is_latest=(rev.number == revisions[-1].number), lines=lines, problems=problems, total=total,
        problems_json=json.dumps(problems), 
        changes=rev.changes.select_related("user")[:200], events=quote.events.select_related("user")[:50],
        zoho_url=zoho.estimate_url(rev.zoho_estimate_id) if rev.zoho_estimate_id else "",
        zoho_connected=ZohoConnection.get().is_connected,
        ai_lines_count=ai_lines_count, ai_warnings=ai_warnings, ai_quote_total=ai_quote_total,
        can_delete=can_delete, has_approved_or_void=has_approved_or_void,
        can_discard=can_discard, can_void=can_void, can_revise=can_revise,
        next_revision_number=next_revision_number,
        quote_ai_count=quote_ai_count, quote_ai_tokens=quote_ai_tokens,
        quote_ai_cost=quote_ai_cost, is_budget_blocked=is_budget_blocked))


@login_required
@require_POST
def revision_save(request, pk):
    rev = get_object_or_404(QuoteRevision, pk=pk)
    try:
        payload = json.loads(request.body or b"{}")
        n = workflow.save_snapshot(rev, payload.get("snapshot"), request.user)
    except (ValueError, workflow.WorkflowError) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    rev.refresh_from_db()
    lines, problems = extract_lines(rev.snapshot)
    return JsonResponse({"ok": True, "changes": n, "lines": len(lines),
                         "total": sum(ln["qty"] * ln["rate"] for ln in lines), "problems": problems})


def _action(request, pk, fn, success, **kw):
    rev = get_object_or_404(QuoteRevision.objects.select_related("quote"), pk=pk)
    try:
        fn(rev, request.user, **kw)
        messages.success(request, success)
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
    return redirect(f"{reverse('vendor_quotes:quote_detail', args=[rev.quote_id])}?rev={rev.number}")


@login_required
@require_POST
def revision_submit(request, pk):
    return _action(request, pk, workflow.submit, "Sent to the GM for approval.")


@login_required
@require_POST
def revision_approve(request, pk):
    return _action(request, pk, workflow.approve, "Approved. The Excel price table and the Zoho draft are being created.",
                   comment=request.POST.get("comment", ""))


@login_required
@require_POST
def revision_send_back(request, pk):
    return _action(request, pk, workflow.send_back, "Sent back with your comment.", comment=request.POST.get("comment", ""))


@login_required
@require_POST
def revision_reject(request, pk):
    return _action(request, pk, workflow.reject, "Quote rejected.", comment=request.POST.get("comment", ""))


@login_required
@require_POST
def quote_revise(request, pk):
    quote = get_object_or_404(VendorQuote, pk=pk)
    try:
        rev = workflow.revise(quote, request.user)
        messages.success(request, f"{rev.label} created from the last approved version. Make the changes, save, and send for approval.")
        return redirect(f"{reverse('vendor_quotes:quote_detail', args=[quote.pk])}?rev={rev.number}")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
        return redirect("vendor_quotes:quote_detail", pk=quote.pk)


@login_required
@require_POST
def revision_retry(request, pk):
    if (resp := _guard(request)):
        return resp
    rev = get_object_or_404(QuoteRevision.objects.select_related("quote"), pk=pk)
    if rev.status != QuoteRevision.STATUS_APPROVED:
        messages.error(request, "Only approved versions create outputs.")
    else:
        from .tasks import run_approval_outputs
        if rev.excel_status != QuoteRevision.OUTPUT_DONE:
            rev.excel_status = QuoteRevision.OUTPUT_PENDING
        if rev.zoho_status != QuoteRevision.OUTPUT_DONE:
            rev.zoho_status = QuoteRevision.OUTPUT_PENDING
        rev.save(update_fields=["excel_status", "zoho_status"])
        run_approval_outputs.delay(rev.id)
        messages.success(request, "Retrying the outputs now.")
    return redirect(f"{reverse('vendor_quotes:quote_detail', args=[rev.quote_id])}?rev={rev.number}")


@login_required
def quote_file(request, pk):
    if (resp := _guard(request)):
        return resp
    quote = get_object_or_404(VendorQuote, pk=pk)
    if not quote.quote_file:
        raise Http404
    return FileResponse(quote.quote_file.open("rb"), as_attachment=request.GET.get("download") == "1",
                        filename=quote.quote_file.name.rsplit("/", 1)[-1])


@login_required
@require_POST
def quote_ai_read(request, pk):
    if (resp := _guard(request)):
        return resp
    quote = get_object_or_404(VendorQuote.objects.select_related("vendor", "enquiry"), pk=pk)
    rev = quote.latest_revision
    if not rev:
        return JsonResponse({"ok": False, "error": "Quote has no revisions."}, status=400)

    if not workflow.can_edit_revision(request.user, rev):
        return JsonResponse({"ok": False, "error": "You do not have permission to edit this revision."}, status=403)

    if rev.status not in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_SENT_BACK):
        return JsonResponse({"ok": False, "error": f"Cannot run AI reading on a {rev.get_status_display().lower()} quote."}, status=400)

    if not quote.quote_file:
        return JsonResponse({"ok": False, "error": "No vendor quote file uploaded."}, status=400)

    try:
        if quote.quote_file.size > 20 * 1024 * 1024:
            return JsonResponse({"ok": False, "error": "Quote file exceeds the 20 MB limit for AI processing."}, status=400)
    except Exception:
        pass

    if workflow.is_ai_budget_blocked():
        return JsonResponse({"ok": False, "error": "Monthly AI budget reached."}, status=400)

    api_key = getattr(settings, "GEMINI_API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return JsonResponse({"ok": False, "error": "Gemini API key is not configured. Set GEMINI_API_KEY on the server."}, status=400)

    if quote.ai_status == VendorQuote.AI_RUNNING:
        return JsonResponse({"ok": True, "status": VendorQuote.AI_RUNNING, "message": "AI extraction is already running."})

    quote.ai_status = VendorQuote.AI_RUNNING
    quote.ai_error = ""
    quote.save(update_fields=["ai_status", "ai_error", "updated_at"])

    from .tasks import read_vendor_quote_with_ai
    read_vendor_quote_with_ai.delay(quote.id, request.user.id)
    return JsonResponse({"ok": True, "status": VendorQuote.AI_RUNNING})


@login_required
def quote_ai_status(request, pk):
    if (resp := _guard(request)):
        return resp
    quote = get_object_or_404(VendorQuote, pk=pk)
    ai_result = quote.ai_result or {}
    lines = ai_result.get("lines") or []
    warnings = ai_result.get("warnings") or []
    quote_total = ai_result.get("quote_total")
    return JsonResponse({
        "ok": True,
        "status": quote.ai_status,
        "error": quote.ai_error,
        "ran_at": quote.ai_ran_at.isoformat() if quote.ai_ran_at else None,
        "lines_count": len(lines),
        "warnings": warnings,
        "quote_total": quote_total,
    })


@login_required
def revision_excel(request, pk):
    if (resp := _guard(request)):
        return resp
    rev = get_object_or_404(QuoteRevision, pk=pk)
    if rev.excel_file:
        return FileResponse(rev.excel_file.open("rb"), as_attachment=True, filename=rev.excel_file.name.rsplit("/", 1)[-1])
    # Not approved yet: export the current sheet on the fly
    from .services.excel_io import build_single_workbook
    import io
    title = workflow.sheet_title_for(rev)
    data = build_single_workbook(rev.snapshot, title)
    return FileResponse(io.BytesIO(data), as_attachment=True, filename=f"{title.replace(' ', '_')}.xlsx")


# ---------------------------------------------------------------- vendors & masters

@login_required
def vendor_list(request):
    if (resp := _guard(request)):
        return resp
    vendors = Vendor.objects.annotate(n_sheets=Count("master_sheets", distinct=True), n_quotes=Count("quotes", distinct=True))
    form = VendorForm(request.POST or None)
    if request.method == "POST":
        if not workflow.can_approve(request.user) and request.user.role not in ("SUPER_ADMIN", "ADMIN"):
            return HttpResponseForbidden("Only admins can add vendors.")
        if form.is_valid():
            v = form.save()
            messages.success(request, f"Vendor {v.name} added. Upload its master workbook next.")
            return redirect("vendor_quotes:vendor_detail", pk=v.pk)
    return render(request, "vendor_quotes/vendor_list.html", _ctx(request, vendors=vendors, form=form))


@login_required
def vendor_detail(request, pk):
    if (resp := _guard(request)):
        return resp
    vendor = get_object_or_404(Vendor, pk=pk)
    form = VendorForm(instance=vendor)
    upload = MasterUploadForm()
    if request.method == "POST":
        if request.POST.get("action") == "upload":
            upload = MasterUploadForm(request.POST, request.FILES)
            if upload.is_valid():
                try:
                    _m, n = workflow.import_vendor_master(vendor, upload.cleaned_data["file"], request.user)
                    messages.success(request, f"Master imported: {n} sheets saved as past quotes.")
                    return redirect("vendor_quotes:vendor_detail", pk=vendor.pk)
                except Exception as e:
                    messages.error(request, f"Couldn't read that workbook: {e}")
        else:
            form = VendorForm(request.POST, instance=vendor)
            if form.is_valid():
                form.save()
                messages.success(request, "Vendor defaults saved.")
                return redirect("vendor_quotes:vendor_detail", pk=vendor.pk)
    q = (request.GET.get("q") or "").strip()
    sheets = vendor.master_sheets.defer("snapshot")
    if q:
        sheets = sheets.filter(Q(enquiry_no__icontains=q) | Q(customer_name__icontains=q) | Q(sheet_name__icontains=q)
                               | Q(vendor_quote_no__icontains=q) | Q(quotation_no__icontains=q))
    if request.GET.get("missing") == "1":
        sheets = sheets.filter(details_missing=True)
    page = Paginator(sheets.order_by("-position"), 30).get_page(request.GET.get("page"))
    can_delete_vendor = workflow.can_delete_vendor(vendor, request.user)
    return render(request, "vendor_quotes/vendor_detail.html", _ctx(
        request, vendor=vendor, form=form, upload=upload, page=page, q=q,
        masters=vendor.master_files.all()[:10], missing=vendor.master_sheets.filter(details_missing=True).count(),
        can_delete_vendor=can_delete_vendor, current_master=vendor.current_master))


@login_required
def master_sheet_detail(request, pk):
    if (resp := _guard(request)):
        return resp
    sheet = get_object_or_404(MasterSheet.objects.select_related("vendor"), pk=pk)
    if request.method == "POST":
        for f in ("enquiry_no", "customer_name", "vendor_quote_no", "quotation_no"):
            setattr(sheet, f, (request.POST.get(f) or "").strip())
        sheet.details_missing = not sheet.enquiry_no
        sheet.save()
        messages.success(request, "Sheet details saved.")
        return redirect("vendor_quotes:master_sheet_detail", pk=sheet.pk)
    can_delete_sheet = workflow.can_delete_master_sheet(sheet, request.user)
    return render(request, "vendor_quotes/master_sheet.html", _ctx(
        request, sheet=sheet, can_delete_sheet=can_delete_sheet))


@login_required
def master_file_download(request, pk):
    if (resp := _guard(request)):
        return resp
    m = get_object_or_404(VendorMasterFile, pk=pk)
    return FileResponse(m.file.open("rb"), as_attachment=True, filename=m.filename)


# ---------------------------------------------------------------- delete / void / restore views

@login_required
@require_POST
def quote_delete(request, pk):
    quote = get_object_or_404(VendorQuote.objects.select_related("enquiry", "vendor"), pk=pk)
    try:
        enquiry_id = quote.enquiry_id
        enquiry_deleted = workflow.delete_quote(quote, request.user)
        if enquiry_deleted:
            messages.success(request, "Vendor quote and empty enquiry deleted.")
            return redirect("vendor_quotes:quote_list")
        messages.success(request, "Vendor quote deleted.")
        return redirect("vendor_quotes:enquiry_detail", pk=enquiry_id)
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
        return redirect("vendor_quotes:quote_detail", pk=pk)


@login_required
@require_POST
def revision_discard(request, pk):
    rev = get_object_or_404(QuoteRevision.objects.select_related("quote"), pk=pk)
    try:
        quote = workflow.discard_revision(rev, request.user)
        messages.success(request, f"Revision {rev.label} discarded.")
        return redirect("vendor_quotes:quote_detail", pk=quote.pk)
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
        return redirect(f"{reverse('vendor_quotes:quote_detail', args=[rev.quote_id])}?rev={rev.number}")


@login_required
@require_POST
def revision_void(request, pk):
    rev = get_object_or_404(QuoteRevision.objects.select_related("quote"), pk=pk)
    comment = request.POST.get("comment", "")
    try:
        workflow.void_revision(rev, request.user, comment)
        messages.success(request, f"Revision {rev.label} marked as void.")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
    return redirect(f"{reverse('vendor_quotes:quote_detail', args=[rev.quote_id])}?rev={rev.number}")


@login_required
@require_POST
def enquiry_delete(request, pk):
    enquiry = get_object_or_404(Enquiry, pk=pk)
    confirm_number = request.POST.get("confirm_number", "")
    try:
        workflow.delete_enquiry(enquiry, request.user, confirm_number)
        messages.success(request, f"Enquiry {enquiry.number} deleted.")
        return redirect("vendor_quotes:quote_list")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
        return redirect("vendor_quotes:enquiry_detail", pk=pk)


@login_required
@require_POST
def vendor_delete(request, pk):
    vendor = get_object_or_404(Vendor, pk=pk)
    try:
        workflow.delete_vendor(vendor, request.user)
        messages.success(request, f"Vendor {vendor.name} deleted.")
        return redirect("vendor_quotes:vendor_list")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
        return redirect("vendor_quotes:vendor_detail", pk=pk)


@login_required
@require_POST
def vendor_toggle_active(request, pk):
    vendor = get_object_or_404(Vendor, pk=pk)
    try:
        workflow.toggle_vendor_active(vendor, request.user)
        status_text = "activated" if vendor.is_active else "deactivated"
        messages.success(request, f"Vendor {vendor.name} {status_text}.")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
    next_url = request.POST.get("next") or reverse("vendor_quotes:vendor_detail", args=[vendor.pk])
    return redirect(next_url)


@login_required
@require_POST
def master_file_delete(request, pk):
    mf = get_object_or_404(VendorMasterFile.objects.select_related("vendor"), pk=pk)
    vendor_id = mf.vendor_id
    try:
        workflow.delete_master_file(mf, request.user)
        messages.success(request, "Historical master file deleted.")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
    return redirect("vendor_quotes:vendor_detail", pk=vendor_id)


@login_required
@require_POST
def master_file_restore(request, pk):
    mf = get_object_or_404(VendorMasterFile.objects.select_related("vendor"), pk=pk)
    vendor_id = mf.vendor_id
    try:
        workflow.restore_master_file(mf, request.user)
        messages.success(request, f"Master file restored from {mf.created_at.strftime('%b %d, %Y %H:%M')}.")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
    return redirect("vendor_quotes:vendor_detail", pk=vendor_id)


@login_required
@require_POST
def master_sheet_delete(request, pk):
    sheet = get_object_or_404(MasterSheet.objects.select_related("vendor"), pk=pk)
    vendor_id = sheet.vendor_id
    try:
        workflow.delete_master_sheet(sheet, request.user)
        messages.success(request, f"Past quote sheet '{sheet.sheet_name}' deleted.")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
    return redirect("vendor_quotes:vendor_detail", pk=vendor_id)


@login_required
@require_POST
def master_sheets_bulk_delete(request, pk):
    vendor = get_object_or_404(Vendor, pk=pk)
    sheet_ids = request.POST.getlist("selected_sheets")
    try:
        n = workflow.bulk_delete_master_sheets(vendor, sheet_ids, request.user)
        messages.success(request, f"{n} past quote sheet(s) deleted.")
    except workflow.WorkflowError as e:
        messages.error(request, str(e))
    return redirect("vendor_quotes:vendor_detail", pk=vendor.pk)


# ---------------------------------------------------------------- Zoho connection

@login_required
def zoho_settings(request):
    if (resp := _guard(request, approve=True)):
        return resp
    conn = ZohoConnection.get()
    waiting = QuoteRevision.objects.filter(zoho_status=QuoteRevision.OUTPUT_WAITING).count()
    return render(request, "vendor_quotes/zoho_settings.html", _ctx(
        request, conn=conn, configured=zoho.is_configured(), redirect_uri=zoho.redirect_uri(),
        org_id=zoho.org_id(), accounts_url=zoho.accounts_url(), waiting=waiting))


@login_required
def zoho_connect(request):
    if (resp := _guard(request, approve=True)):
        return resp
    if not zoho.is_configured():
        messages.error(request, "Add ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET to the server .env first.")
        return redirect("vendor_quotes:zoho_settings")
    state = secrets.token_urlsafe(24)
    request.session["zoho_oauth_state"] = state
    return redirect(zoho.authorize_url(state))


@login_required
def zoho_callback(request):
    if (resp := _guard(request, approve=True)):
        return resp
    if request.GET.get("state") != request.session.pop("zoho_oauth_state", None):
        messages.error(request, "Zoho connection expired or was not started here. Please try again.")
        return redirect("vendor_quotes:zoho_settings")
    if request.GET.get("error"):
        messages.error(request, f"Zoho said: {request.GET['error']}")
        return redirect("vendor_quotes:zoho_settings")
    try:
        zoho.exchange_code(request.GET.get("code", ""), request.user)
    except zoho.ZohoError as e:
        messages.error(request, f"Couldn't connect Zoho: {e}")
        return redirect("vendor_quotes:zoho_settings")
    from .tasks import retry_waiting_zoho_drafts
    retry_waiting_zoho_drafts.delay()
    messages.success(request, "Zoho Books connected. Waiting approvals are being sent to Zoho now.")
    return redirect("vendor_quotes:zoho_settings")


@login_required
@require_POST
def zoho_disconnect(request):
    if (resp := _guard(request, approve=True)):
        return resp
    zoho.disconnect()
    messages.success(request, "Zoho Books disconnected.")
    return redirect("vendor_quotes:zoho_settings")


@login_required
def zoho_customer_search(request):
    if (resp := _guard(request)):
        return resp
    text = (request.GET.get("q") or "").strip()
    if len(text) < 2:
        return JsonResponse({"results": []})
    try:
        return JsonResponse({"results": zoho.search_customers(text)})
    except zoho.ZohoError as e:
        return JsonResponse({"results": [], "error": str(e)}, status=200)


def _guard_ai_usage(request):
    is_allowed = (
        workflow.can_approve(request.user)
        or getattr(request.user, "role", None) in ("SUPER_ADMIN", "ADMIN")
        or getattr(request.user, "is_superuser", False)
    )
    return None if is_allowed else HttpResponseForbidden("Only admins and approvers can access AI usage.")


def _generate_csv_response(qs, filename="ai_usage.csv"):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([
        "Timestamp (UTC)",
        "User",
        "Email",
        "Quote ID",
        "Vendor",
        "Vendor Quote No",
        "Enquiry",
        "Feature",
        "Provider",
        "Model",
        "File Name",
        "File Type",
        "File Size (Bytes)",
        "Status",
        "Duration (Seconds)",
        "Input Tokens",
        "Output Tokens",
        "Total Tokens",
        "Lines Extracted",
        "Warnings Count",
        "Cost (USD)",
        "Error Message",
    ])
    for item in qs.order_by("-created_at"):
        writer.writerow([
            item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            item.user.full_name if item.user else "System",
            item.user.email if item.user else "",
            item.quote_id or "",
            item.quote.vendor.name if item.quote and item.quote.vendor else "",
            item.quote.vendor_quote_no if item.quote else "",
            item.quote.enquiry.number if item.quote and item.quote.enquiry else "",
            item.feature,
            item.provider,
            item.model,
            item.file_name,
            item.file_type,
            item.file_size_bytes,
            item.status,
            round(item.duration_ms / 1000.0, 3),
            item.input_tokens,
            item.output_tokens,
            item.total_tokens,
            item.lines_extracted,
            item.warnings_count,
            f"{item.estimated_cost_usd:.6f}",
            item.error_message,
        ])
    return response


@login_required
def ai_usage(request):
    if (resp := _guard_ai_usage(request)):
        return resp

    if request.GET.get("export") == "csv":
        return ai_usage_export_csv(request)

    now = timezone.now()
    today = now.date()

    # Month filter
    month_param = (request.GET.get("month") or "").strip()
    if not month_param:
        month_param = now.strftime("%Y-%m")

    filter_year = None
    filter_month = None
    if month_param != "all":
        try:
            parts = month_param.split("-")
            filter_year = int(parts[0])
            filter_month = int(parts[1])
        except (ValueError, IndexError):
            filter_year = now.year
            filter_month = now.month
            month_param = now.strftime("%Y-%m")

    user_id = (request.GET.get("user_id") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().upper()

    # Base QS for recent requests table
    qs = AIRequestLog.objects.select_related("user", "quote__vendor", "quote__enquiry")
    if filter_year and filter_month:
        qs = qs.filter(created_at__year=filter_year, created_at__month=filter_month)
    if user_id:
        qs = qs.filter(user_id=user_id)
    if status_filter in (AIRequestLog.STATUS_SUCCESS, AIRequestLog.STATUS_FAILED):
        qs = qs.filter(status=status_filter)

    # Top summary cards (current month & today)
    requests_today = AIRequestLog.objects.filter(created_at__date=today).count()
    month_qs = AIRequestLog.objects.filter(created_at__year=now.year, created_at__month=now.month)
    requests_month = month_qs.count()
    success_month = month_qs.filter(status=AIRequestLog.STATUS_SUCCESS).count()
    success_rate_month = round((success_month / requests_month * 100), 1) if requests_month > 0 else 100.0

    tokens_month = month_qs.aggregate(s=Sum("total_tokens"))["s"] or 0
    cost_month_usd = month_qs.aggregate(s=Sum("estimated_cost_usd"))["s"] or Decimal("0")
    usd_to_idr = getattr(settings, "AI_USD_TO_IDR", Decimal("16000"))
    cost_month_idr = cost_month_usd * usd_to_idr

    avg_ms = month_qs.aggregate(a=Avg("duration_ms"))["a"] or 0
    avg_duration_seconds = round(avg_ms / 1000.0, 2)

    budget_usd = getattr(settings, "AI_MONTHLY_BUDGET_USD", Decimal("0"))
    budget_pct = int(round((cost_month_usd / budget_usd) * 100)) if budget_usd > 0 else 0
    is_budget_blocked = workflow.is_ai_budget_blocked()

    # 30-Day Trend Chart
    past_30_start = today - timedelta(days=29)
    trend_qs = AIRequestLog.objects.filter(created_at__date__gte=past_30_start)
    from collections import defaultdict
    daily_stats = defaultdict(lambda: {"count": 0, "cost": Decimal("0")})
    for log in trend_qs.only("created_at", "estimated_cost_usd"):
        d = log.created_at.date()
        daily_stats[d]["count"] += 1
        daily_stats[d]["cost"] += log.estimated_cost_usd

    chart_labels = []
    chart_requests = []
    chart_costs = []
    for i in reversed(range(30)):
        d = today - timedelta(days=i)
        chart_labels.append(d.strftime("%b %d"))
        chart_requests.append(daily_stats[d]["count"])
        chart_costs.append(float(round(daily_stats[d]["cost"], 4)))

    daily_chart_json = json.dumps({
        "labels": chart_labels,
        "requests": chart_requests,
        "costs": chart_costs,
    })

    # Table "By user" for the selected month
    target_year = filter_year or now.year
    target_month = filter_month or now.month
    by_user_qs = (
        AIRequestLog.objects.filter(created_at__year=target_year, created_at__month=target_month)
        .values("user__id", "user__full_name", "user__email")
        .annotate(
            total_req=Count("id"),
            failed_req=Count("id", filter=Q(status=AIRequestLog.STATUS_FAILED)),
            tokens=Sum("total_tokens"),
            cost=Sum("estimated_cost_usd"),
        )
        .order_by("-cost", "-total_req")
    )

    # Pagination for recent requests table
    paginator = Paginator(qs.order_by("-created_at"), 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Past 12 months for month dropdown
    month_options = []
    for i in range(12):
        m_date = (today.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        val = m_date.strftime("%Y-%m")
        if val not in [o["value"] for o in month_options]:
            month_options.append({"value": val, "label": m_date.strftime("%B %Y")})

    filter_users = User.objects.filter(ai_request_logs__isnull=False).distinct().order_by("full_name", "email")

    return render(
        request,
        "vendor_quotes/ai_usage.html",
        _ctx(
            request,
            nav="ai_usage",
            requests_today=requests_today,
            requests_month=requests_month,
            success_rate_month=success_rate_month,
            tokens_month=tokens_month,
            cost_month_usd=cost_month_usd,
            cost_month_idr=cost_month_idr,
            avg_duration_seconds=avg_duration_seconds,
            budget_usd=budget_usd,
            budget_pct=budget_pct,
            is_budget_blocked=is_budget_blocked,
            daily_chart_json=daily_chart_json,
            by_user=by_user_qs,
            page_obj=page_obj,
            month_options=month_options,
            filter_users=filter_users,
            selected_month=month_param,
            selected_user_id=user_id,
            selected_status=status_filter,
            target_year=target_year,
            target_month=target_month,
        ),
    )


@login_required
def ai_usage_export_csv(request):
    if (resp := _guard_ai_usage(request)):
        return resp

    now = timezone.now()
    month_param = (request.GET.get("month") or "").strip()
    user_id = (request.GET.get("user_id") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().upper()

    qs = AIRequestLog.objects.select_related("user", "quote__vendor", "quote__enquiry")
    if month_param and month_param != "all":
        try:
            parts = month_param.split("-")
            qs = qs.filter(created_at__year=int(parts[0]), created_at__month=int(parts[1]))
        except (ValueError, IndexError):
            pass
    if user_id:
        qs = qs.filter(user_id=user_id)
    if status_filter in (AIRequestLog.STATUS_SUCCESS, AIRequestLog.STATUS_FAILED):
        qs = qs.filter(status=status_filter)

    fname_suffix = month_param if month_param else now.strftime("%Y-%m")
    return _generate_csv_response(qs, filename=f"ai_usage_{fname_suffix}.csv")
