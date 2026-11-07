import json
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import MasterUploadForm, QuoteCreateForm, VendorForm
from .models import (Enquiry, MasterSheet, QuoteRevision, Vendor, VendorMasterFile, VendorQuote,
                     ZohoConnection)
from .services import workflow, zoho
from .services.sheet import extract_lines


def _guard(request, approve=False):
    ok = workflow.can_approve(request.user) if approve else workflow.can_edit(request.user)
    return None if ok else HttpResponseForbidden("You don't have access to Vendor Quotes.")


def _ctx(request, **extra):
    extra.setdefault("can_approve", workflow.can_approve(request.user))
    extra.setdefault("can_edit", workflow.can_edit(request.user))
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
    return render(request, "vendor_quotes/enquiry_detail.html", _ctx(
        request, enquiry=enquiry, quotes=quotes, zoho_connected=ZohoConnection.get().is_connected))


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
    return render(request, "vendor_quotes/quote_detail.html", _ctx(
        request, quote=quote, rev=rev, revisions=revisions, editable=editable,
        is_latest=(rev.number == revisions[-1].number), lines=lines, problems=problems, total=total,
        problems_json=json.dumps(problems), 
        changes=rev.changes.select_related("user")[:200], events=quote.events.select_related("user")[:50],
        zoho_url=zoho.estimate_url(rev.zoho_estimate_id) if rev.zoho_estimate_id else "",
        zoho_connected=ZohoConnection.get().is_connected))


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
    return render(request, "vendor_quotes/vendor_detail.html", _ctx(
        request, vendor=vendor, form=form, upload=upload, page=page, q=q,
        masters=vendor.master_files.all()[:10], missing=vendor.master_sheets.filter(details_missing=True).count()))


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
    return render(request, "vendor_quotes/master_sheet.html", _ctx(request, sheet=sheet))


@login_required
def master_file_download(request, pk):
    if (resp := _guard(request)):
        return resp
    m = get_object_or_404(VendorMasterFile, pk=pk)
    return FileResponse(m.file.open("rb"), as_attachment=True, filename=m.filename)


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
