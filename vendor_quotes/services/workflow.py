"""Business actions for vendor quotes: create, save, submit, approve, send back, reject, revise."""
import logging
import os
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..models import (AIBudgetNotification, AIRequestLog, Enquiry, MasterSheet, QuoteChange,
                      QuoteEvent, QuoteRevision, Vendor, VendorMasterFile, VendorQuote)
from . import zoho
from .excel_io import append_to_master, build_single_workbook, import_master
from .sheet import (build_standard_snapshot, cell_number, cell_text, diff_snapshots, extract_lines,
                    first_sheet, fresh_copy, get_cell, is_standard_layout, normalize_currency, resolve_rate)

logger = logging.getLogger(__name__)
User = get_user_model()

EDITOR_ROLES = ("SUPER_ADMIN", "ADMIN", "DEPARTMENT_ADMIN")
EDITOR_GROUP = "Vendor Quotes"
APPROVER_GROUP = "Quote Approvers"


class WorkflowError(Exception):
    pass


# ---------------------------------------------------------------- permissions

def approver_roles():
    return tuple(getattr(settings, "VENDOR_QUOTE_APPROVER_ROLES", ("SUPER_ADMIN",)))


def _group_names(user):
    """User's group names, fetched once per user object (keeps page query counts low)."""
    if not hasattr(user, "_vq_group_names"):
        user._vq_group_names = set(user.groups.values_list("name", flat=True))
    return user._vq_group_names


def can_approve(user):
    if not user.is_authenticated:
        return False
    if getattr(user, "role", None) in approver_roles() or user.is_superuser:
        return True
    return APPROVER_GROUP in _group_names(user)


def can_edit(user):
    if not user.is_authenticated:
        return False
    if getattr(user, "role", None) in EDITOR_ROLES or can_approve(user):
        return True
    return EDITOR_GROUP in _group_names(user)


def approvers():
    qs = User.objects.filter(is_active=True)
    from django.db.models import Q
    return qs.filter(Q(role__in=approver_roles()) | Q(groups__name=APPROVER_GROUP)).distinct()


def can_edit_revision(user, rev):
    if rev.status in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_SENT_BACK):
        return can_edit(user)
    if rev.status == QuoteRevision.STATUS_IN_REVIEW:
        return can_approve(user)   # the GM can correct prices while reviewing
    return False


# ---------------------------------------------------------------- notifications

def _site():
    return getattr(settings, "SITE_URL", "https://flowforceworkspace.cloud").rstrip("/")


def notify(users, title, message, quote=None, kind="SYSTEM"):
    """Workspace bell + email (sent from DEFAULT_FROM_EMAIL via the existing EmailLog queue)."""
    from tasks.models import EmailLog, Notification
    from tasks.tasks import send_email_log_task
    if quote:
        link = reverse("vendor_quotes:quote_detail", args=[quote.id])
    else:
        link = reverse("vendor_quotes:ai_usage")
    for u in {u.id: u for u in users if u and u.is_active}.values():
        Notification.objects.create(user=u, title=title, description=message, type=kind, link=link)
        if not u.email:
            continue
        html = render_to_string("vendor_quotes/email_notice.html", {
            "name": u.full_name or u.email, "title": title, "message": message,
            "quote": quote, "url": _site() + link,
        })
        log = EmailLog.objects.create(recipient_email=u.email, subject=f"[Flow-Force] {title}", body=html,
                                      email_type="VENDOR_QUOTE_MAIL", status="PENDING", max_retries=3)
        transaction.on_commit(lambda log_id=log.id: send_email_log_task.delay(log_id))


def check_ai_budget_alerts(quote=None):
    """
    Check if current month's AI usage has crossed 80% or 100% of AI_MONTHLY_BUDGET_USD.
    Sends at most one notification per threshold per month to approvers.
    """
    try:
        from django.db.models import Sum
        budget = getattr(settings, "AI_MONTHLY_BUDGET_USD", Decimal("0"))
        if not isinstance(budget, Decimal):
            budget = Decimal(str(budget))
        if budget <= 0:
            return

        now = timezone.now()
        year_month = now.strftime("%Y-%m")

        total_cost = AIRequestLog.objects.filter(
            created_at__year=now.year,
            created_at__month=now.month,
            status=AIRequestLog.STATUS_SUCCESS,
        ).aggregate(s=Sum("estimated_cost_usd"))["s"] or Decimal("0")

        # 100% threshold
        if total_cost >= budget:
            _send_budget_alert(100, year_month, total_cost, budget, quote)
        # 80% threshold
        elif total_cost >= budget * Decimal("0.8"):
            _send_budget_alert(80, year_month, total_cost, budget, quote)

    except Exception as e:
        logger.warning("Could not check AI budget alerts: %s", e)


def _send_budget_alert(threshold, year_month, total_cost, budget, quote=None):
    created = False
    try:
        _, created = AIBudgetNotification.objects.get_or_create(
            year_month=year_month,
            threshold=threshold,
        )
    except Exception:
        return

    if created:
        approver_list = list(approvers())
        pct = int(round((total_cost / budget) * 100))
        title = f"AI Budget Alert: {threshold}% Reached"
        message = (
            f"Monthly AI spend for {year_month} has reached ${total_cost:.4f} USD "
            f"({pct}% of the ${budget:.2f} monthly budget). Threshold {threshold}% crossed."
        )
        notify(approver_list, title, message, quote=quote, kind="WARNING")


def is_ai_budget_blocked():
    """
    Returns True if AI_BLOCK_OVER_BUDGET is True and current month's cost >= AI_MONTHLY_BUDGET_USD > 0.
    """
    from django.db.models import Sum

    budget = getattr(settings, "AI_MONTHLY_BUDGET_USD", Decimal("0"))
    if not isinstance(budget, Decimal):
        budget = Decimal(str(budget))
    block = getattr(settings, "AI_BLOCK_OVER_BUDGET", False)

    if not block or budget <= 0:
        return False

    now = timezone.now()
    total_cost = AIRequestLog.objects.filter(
        created_at__year=now.year,
        created_at__month=now.month,
        status=AIRequestLog.STATUS_SUCCESS,
    ).aggregate(s=Sum("estimated_cost_usd"))["s"] or Decimal("0")

    return total_cost >= budget


def event(quote, action, message="", user=None, revision=None):
    QuoteEvent.objects.create(quote=quote, revision=revision, user=user, action=action, message=message)


def log_workspace_activity(user, action, message):
    """
    Log an entry to the workspace's activity log (EmployeeActivityLog).
    """
    try:
        from employee_management.models import EmployeeActivityLog
        if user and user.is_authenticated:
            act = action if action in dict(EmployeeActivityLog.ACTIVITY_TYPES) else "DELETE"
            EmployeeActivityLog.objects.create(
                employee=user,
                activity_type=act,
                performed_by=user,
                description=message,
            )
    except Exception as e:
        logger.warning("Could not write to EmployeeActivityLog: %s", e)


# ---------------------------------------------------------------- vendors / masters

@transaction.atomic
def import_vendor_master(vendor, uploaded_file, user, note="Imported master"):
    uploaded_file.seek(0)
    sheets = import_master(uploaded_file)
    uploaded_file.seek(0)
    master = VendorMasterFile.objects.create(vendor=vendor, file=uploaded_file, uploaded_by=user,
                                             note=note, sheet_count=len(sheets))
    vendor.master_sheets.all().delete()
    MasterSheet.objects.bulk_create([
        MasterSheet(vendor=vendor, source=master, sheet_name=s["name"], position=s["position"],
                    enquiry_no=s["details"]["enquiry_no"], customer_name=s["details"]["customer_name"],
                    vendor_quote_no=s["details"]["vendor_quote_no"], quotation_no=s["details"]["quotation_no"],
                    snapshot=s["snapshot"], details_missing=not s["details"]["enquiry_no"])
        for s in sheets
    ])
    return master, len(sheets)


# ---------------------------------------------------------------- quotes

@transaction.atomic
def create_quote(*, enquiry_number, customer_name, vendor, vendor_quote_no, quote_file, user,
                 start_from=None, currency=None):
    enquiry_number = enquiry_number.strip().upper()
    enquiry, created = Enquiry.objects.get_or_create(
        number=enquiry_number, defaults={"customer_name": customer_name.strip(), "created_by": user})
    if not created and customer_name and not enquiry.customer_name:
        enquiry.customer_name = customer_name.strip()
        enquiry.save(update_fields=["customer_name"])
    quote = VendorQuote.objects.create(enquiry=enquiry, vendor=vendor, vendor_quote_no=vendor_quote_no.strip(),
                                       currency=normalize_currency(currency or vendor.currency), created_by=user)
    if quote_file:
        quote.quote_file = quote_file
        quote.save(update_fields=["quote_file"])
    title = f"{enquiry.number} {vendor.code or vendor.name}"
    if start_from is not None:
        snapshot = fresh_copy(start_from, title=title)
    else:
        snapshot = build_standard_snapshot(
            enquiry_no=enquiry.number, customer=enquiry.customer_name, vendor_name=vendor.name,
            vendor_quote_no=quote.vendor_quote_no, currency=quote.currency,
            exchange_rate=resolve_rate(quote.currency, vendor), margin_factor=vendor.default_margin_factor,
            duty_factor=vendor.default_duty_factor, freight_factor=vendor.default_freight_factor,
            discount=vendor.default_discount, title=title)
    rev = QuoteRevision.objects.create(quote=quote, number=0, snapshot=snapshot, created_by=user)
    event(quote, "created", f"Vendor quote uploaded for {enquiry.number}", user, rev)
    return quote, rev


@transaction.atomic
def save_snapshot(rev, snapshot, user):
    rev = QuoteRevision.objects.select_for_update().get(pk=rev.pk)
    if not can_edit_revision(user, rev):
        raise WorkflowError("This version can't be edited any more.")
    if not isinstance(snapshot, dict) or not snapshot.get("sheets"):
        raise WorkflowError("The sheet data was empty - nothing saved.")
    changes = diff_snapshots(rev.snapshot, snapshot)
    QuoteChange.objects.bulk_create([
        QuoteChange(revision=rev, user=user, sheet=s[:100], cell=c, old_value=o[:2000], new_value=n[:2000])
        for s, c, o, n in changes
    ])
    rev.snapshot = snapshot
    rev.save(update_fields=["snapshot", "updated_at"])
    return len(changes)


@transaction.atomic
def submit(rev, user):
    rev = QuoteRevision.objects.select_for_update().get(pk=rev.pk)
    if rev.status not in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_SENT_BACK):
        raise WorkflowError("Only a draft can be sent for approval.")
    lines, problems = extract_lines(rev.snapshot)
    if problems and not lines:
        raise WorkflowError(problems[0])
    rev.status = QuoteRevision.STATUS_IN_REVIEW
    rev.submitted_at = timezone.now()
    rev.save(update_fields=["status", "submitted_at", "updated_at"])
    q = rev.quote
    event(q, "submitted", f"{rev.label} sent for approval ({len(lines)} lines)", user, rev)
    notify(approvers(), f"Quote waiting for approval: {q.enquiry.number}",
           f"{user.full_name or user.email} sent {q.vendor.name} {rev.label} for {q.enquiry.customer_name} for your approval.",
           q, kind="REVIEW")
    return rev


def _decide(rev, user, status, comment, action):
    if not can_approve(user):
        raise WorkflowError("Only the GM / approvers can do this.")
    rev = QuoteRevision.objects.select_for_update().get(pk=rev.pk)
    if rev.status != QuoteRevision.STATUS_IN_REVIEW:
        raise WorkflowError("This version is not waiting for approval.")
    rev.status = status
    rev.decided_by = user
    rev.decided_at = timezone.now()
    rev.decision_comment = comment or ""
    rev.save(update_fields=["status", "decided_by", "decided_at", "decision_comment", "updated_at"])
    verb = {"approved": "approved", "sent_back": "sent back", "rejected": "rejected"}[action]
    event(rev.quote, action, f"{rev.label} {verb}" + (f": {comment}" if comment else ""), user, rev)
    return rev


@transaction.atomic
def approve(rev, user, comment=""):
    lines, problems = extract_lines(rev.snapshot)
    if not lines:
        raise WorkflowError(problems[0] if problems else "No priced lines found.")
    rev = _decide(rev, user, QuoteRevision.STATUS_APPROVED, comment, "approved")
    rev.excel_status = QuoteRevision.OUTPUT_PENDING
    rev.zoho_status = QuoteRevision.OUTPUT_PENDING
    rev.save(update_fields=["excel_status", "zoho_status"])
    q = rev.quote
    notify([q.created_by, rev.created_by], f"Quote approved: {q.enquiry.number}",
           f"{user.full_name or user.email} approved {q.vendor.name} {rev.label}. The Excel price table and the Zoho draft are being created.",
           q)
    from ..tasks import run_approval_outputs
    transaction.on_commit(lambda: run_approval_outputs.delay(rev.id))
    return rev


@transaction.atomic
def send_back(rev, user, comment):
    if not (comment or "").strip():
        raise WorkflowError("Please write what needs to be changed.")
    rev = _decide(rev, user, QuoteRevision.STATUS_SENT_BACK, comment, "sent_back")
    q = rev.quote
    notify([q.created_by, rev.created_by], f"Quote sent back: {q.enquiry.number}",
           f"{user.full_name or user.email} sent {q.vendor.name} {rev.label} back: {comment}", q)
    return rev


@transaction.atomic
def reject(rev, user, comment=""):
    rev = _decide(rev, user, QuoteRevision.STATUS_REJECTED, comment, "rejected")
    q = rev.quote
    notify([q.created_by, rev.created_by], f"Quote rejected: {q.enquiry.number}",
           f"{user.full_name or user.email} rejected {q.vendor.name} {rev.label}. {comment}".strip(), q)
    return rev


@transaction.atomic
def revise(quote, user):
    if not can_edit(user):
        raise WorkflowError("You don't have permission to revise quotes.")
    latest = quote.revisions.select_for_update().order_by("-number").first()
    if not latest:
        raise WorkflowError("No revisions found for this quote.")
    if latest.status in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_IN_REVIEW, QuoteRevision.STATUS_SENT_BACK):
        raise WorkflowError("A revision is already in progress. Finish, submit or discard it first.")

    last_approved = quote.revisions.filter(status=QuoteRevision.STATUS_APPROVED).order_by("-number").first()
    if not last_approved:
        raise WorkflowError("You can only revise an approved version, but no approved version was found.")

    import copy
    rev = QuoteRevision.objects.create(
        quote=quote,
        number=latest.number + 1,
        snapshot=fresh_copy(last_approved.snapshot),
        created_by=user,
        quote_subject=last_approved.quote_subject,
        validity_days=last_approved.validity_days,
        salesperson_name=last_approved.salesperson_name,
        salesperson_id=last_approved.salesperson_id,
        delivery_time=last_approved.delivery_time,
        payment_terms=last_approved.payment_terms,
        customer_notes=last_approved.customer_notes,
        extra_terms=last_approved.extra_terms,
        line_details=copy.deepcopy(last_approved.line_details or {}),
    )
    event(quote, "revised", f"{rev.label} started from {last_approved.label}", user, rev)
    return rev


# ---------------------------------------------------------------- approval outputs

def sheet_title_for(rev):
    q = rev.quote
    base = q.enquiry.number.replace("/", " ")
    return base if rev.number == 0 else f"{base} - R{rev.number}"


def run_excel_output(rev):
    q = rev.quote
    title = sheet_title_for(rev)
    try:
        single = build_single_workbook(rev.snapshot, title)
        rev.excel_file.save(f"{title.replace(' ', '_')}.xlsx", ContentFile(single), save=False)
        current = q.vendor.current_master
        master_bytes = None
        if current and current.file:
            with current.file.open("rb") as fh:
                master_bytes = fh.read()
        new_bytes, sheet_name, sheet_count = append_to_master(master_bytes, rev.snapshot, title)
        base = (current.filename.rsplit(".", 1)[0] if current else f"{q.vendor.code or q.vendor.name}_master")
        base = base.split("__v")[0]
        stamp = timezone.now().strftime("%Y%m%d-%H%M")
        new_master = VendorMasterFile(vendor=q.vendor, note=f"Added {sheet_name} (approved {rev.label})",
                                      sheet_count=sheet_count)
        new_master.file.save(f"{base}__v{stamp}.xlsx", ContentFile(new_bytes), save=False)
        new_master.save()
        MasterSheet.objects.create(vendor=q.vendor, source=new_master, sheet_name=sheet_name,
                                   position=new_master.sheet_count, enquiry_no=q.enquiry.number,
                                   customer_name=q.enquiry.customer_name, vendor_quote_no=q.vendor_quote_no,
                                   quotation_no=rev.zoho_estimate_number, snapshot=rev.snapshot)
        rev.master_sheet_name = sheet_name
        rev.excel_status = QuoteRevision.OUTPUT_DONE
        rev.excel_error = ""
        event(q, "excel_done", f"Sheet '{sheet_name}' added to the {q.vendor.name} master", None, rev)
    except Exception as e:  # keep the approval; allow retry
        logger.exception("Excel output failed for revision %s", rev.id)
        rev.excel_status = QuoteRevision.OUTPUT_FAILED
        rev.excel_error = str(e)[:1000]
        event(q, "excel_failed", rev.excel_error, None, rev)
    rev.save()


def run_zoho_output(rev):
    q = rev.quote
    if rev.zoho_estimate_id:
        rev.zoho_status = QuoteRevision.OUTPUT_DONE
        rev.save(update_fields=["zoho_status"])
        return
    lines, problems = extract_lines(rev.snapshot)
    try:
        if not lines:
            raise zoho.ZohoError(problems[0] if problems else "No priced lines found.")
        estimate_number = None
        if rev.number > 0:
            original = q.revisions.filter(number=0).first()
            base = original.zoho_estimate_number if original else ""
            if not base:
                raise zoho.ZohoError("The original quotation has no Zoho number yet - create it first, then retry this revision.")
            estimate_number = zoho.revision_number_for(base, rev.number)
        customer_id = zoho.resolve_customer(q.enquiry)

        # Fallback subject if still empty
        if not rev.quote_subject and lines:
            first_desc = lines[0].get("description", "")
            if first_desc:
                rev.quote_subject = first_desc[:255]
                rev.save(update_fields=["quote_subject"])

        res = zoho.create_draft_estimate(
            customer_id=customer_id,
            reference=q.enquiry.number,
            lines=lines,
            estimate_number=estimate_number,
            quote_subject=rev.quote_subject,
            validity_days=rev.validity_days,
            salesperson_name=rev.salesperson_name,
            salesperson_id=rev.salesperson_id,
            delivery_time=rev.delivery_time,
            payment_terms=rev.payment_terms,
            customer_notes=rev.customer_notes,
            extra_terms=rev.extra_terms,
            line_details=rev.line_details,
        )
        est_id = res[0]
        est_no = res[1]
        stick_warnings = getattr(res, "stick_warnings", [])
        if not stick_warnings and len(res) >= 3:
            stick_warnings = res[2]

        rev.zoho_estimate_id, rev.zoho_estimate_number = est_id, est_no or (estimate_number or "")
        rev.zoho_status = QuoteRevision.OUTPUT_DONE
        rev.zoho_error = ""
        event(q, "zoho_done", f"Draft quotation {rev.zoho_estimate_number} created in Zoho Books", None, rev)
        if stick_warnings:
            event(q, "zoho_warning", f"Draft quotation {rev.zoho_estimate_number} created in Zoho Books, but {' and '.join(stick_warnings)} did not stick.", None, rev)
        MasterSheet.objects.filter(vendor=q.vendor, sheet_name=rev.master_sheet_name, quotation_no="") \
            .update(quotation_no=rev.zoho_estimate_number)
    except zoho.ZohoNotConnected as e:
        rev.zoho_status = QuoteRevision.OUTPUT_WAITING
        rev.zoho_error = str(e)
    except Exception as e:
        logger.exception("Zoho output failed for revision %s", rev.id)
        rev.zoho_status = QuoteRevision.OUTPUT_FAILED
        rev.zoho_error = str(e)[:1000]
        event(q, "zoho_failed", rev.zoho_error, None, rev)
    rev.save()


def run_outputs(rev, excel=True, zoho_draft=True):
    if excel and rev.excel_status != QuoteRevision.OUTPUT_DONE:
        run_excel_output(rev)
    if zoho_draft and rev.zoho_status != QuoteRevision.OUTPUT_DONE:
        run_zoho_output(rev)
    return rev


@transaction.atomic
def apply_ai_result(quote, rev, ai_result, user):
    """Apply structured AI extraction results to an editable quote revision."""
    rev = QuoteRevision.objects.select_for_update().get(pk=rev.pk)
    if rev.status not in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_SENT_BACK):
        raise WorkflowError("AI reading can only be applied to editable revisions (Draft or Sent back).")
    if user and not can_edit_revision(user, rev):
        raise WorkflowError("You do not have permission to edit this revision.")

    # 1. Update quote vendor_quote_no if empty
    extracted_vq_no = (ai_result.get("vendor_quote_no") or "").strip()
    if extracted_vq_no and not quote.vendor_quote_no:
        quote.vendor_quote_no = extracted_vq_no
        quote.save(update_fields=["vendor_quote_no", "updated_at"])

    # 2. Currency: what the AI detected on the quote (INR, USD, EUR ...) wins over the vendor default,
    #    because the same vendor can quote in different currencies (e.g. Rotolok INR or USD).
    old_snap = rev.snapshot or {}
    sh = first_sheet(old_snap)
    standard = is_standard_layout(sh)
    old_cur = normalize_currency(cell_text(get_cell(sh, 4, 2))) if standard else ""
    extracted_cur = normalize_currency(ai_result.get("currency"))
    if extracted_cur and extracted_cur != quote.currency:
        quote.currency = extracted_cur
        quote.save(update_fields=["currency", "updated_at"])
    cur = quote.currency or normalize_currency(quote.vendor.currency) or "EUR"

    # 3. Factors: keep what the user already typed in a standard sheet. A sheet copied from an
    #    old vendor master has a different layout, so its cells are NOT read by position.
    margin_factor = quote.vendor.default_margin_factor
    duty_factor = quote.vendor.default_duty_factor
    freight_factor = quote.vendor.default_freight_factor
    freight = 0
    quote_total = ""
    typed_rate = None

    if standard:
        typed_rate = cell_number(get_cell(sh, 0, 9))
        c_margin = cell_number(get_cell(sh, 1, 9))
        if c_margin is not None and c_margin > 0:
            margin_factor = c_margin
        c_duty = cell_number(get_cell(sh, 2, 9))
        if c_duty is not None and c_duty > 0:
            duty_factor = c_duty
        c_ffactor = cell_number(get_cell(sh, 3, 9))
        if c_ffactor is not None and c_ffactor > 0:
            freight_factor = c_ffactor
        if old_cur == cur:   # freight / total are in quote currency - only reuse if it didn't change
            c_freight = cell_number(get_cell(sh, 4, 9))
            if c_freight is not None:
                freight = c_freight
            c_total = cell_number(get_cell(sh, 5, 9))
            if c_total is not None:
                quote_total = c_total

    exchange_rate = resolve_rate(cur, quote.vendor, current=typed_rate, current_currency=old_cur)

    # Overwrite freight and quote_total if AI found them
    if ai_result.get("freight_or_courier") is not None:
        freight = ai_result["freight_or_courier"]
    if ai_result.get("quote_total") is not None:
        quote_total = ai_result["quote_total"]

    # 4. Map lines
    ai_lines = ai_result.get("lines") or []
    sheet_lines = []
    for ln in ai_lines:
        disc = ln.get("discount_percent")
        # discount as a fraction (e.g. 15 -> 0.15)
        if disc is not None:
            disc_fraction = round(float(disc) / 100.0, 4)
        else:
            disc_fraction = float(quote.vendor.default_discount)

        sheet_lines.append({
            "part": ln.get("part_no") or "",
            "description": ln.get("description") or "",
            "qty": ln.get("qty") if ln.get("qty") is not None else "",
            "unit": ln.get("unit") or "pcs",
            "unit_price": ln.get("unit_price") if ln.get("unit_price") is not None else "",
            "discount": disc_fraction,
        })

    title = f"{quote.enquiry.number} {quote.vendor.code or quote.vendor.name}"

    new_snap = build_standard_snapshot(
        enquiry_no=quote.enquiry.number,
        customer=quote.enquiry.customer_name,
        vendor_name=quote.vendor.name,
        vendor_quote_no=quote.vendor_quote_no,
        currency=cur,
        exchange_rate=exchange_rate,
        margin_factor=margin_factor,
        duty_factor=duty_factor,
        freight_factor=freight_factor,
        discount=quote.vendor.default_discount,
        lines=sheet_lines,
        blank_lines=max(10, len(sheet_lines)),
        title=title,
        freight=freight,
        quote_total=quote_total,
    )

    # 5. Compute diff and record QuoteChange
    changes = diff_snapshots(old_snap, new_snap)
    QuoteChange.objects.bulk_create([
        QuoteChange(
            revision=rev,
            user=user,
            sheet=s[:100],
            cell=c,
            old_value=o[:2000],
            new_value=n[:2000]
        )
        for s, c, o, n in changes
    ])

    # 6. Apply quotation details (subject, delivery time, per-line commodity codes)
    extracted_subj = (ai_result.get("quote_subject") or "").strip()
    if extracted_subj:
        rev.quote_subject = extracted_subj[:255]
    elif not rev.quote_subject and sheet_lines:
        first_desc = sheet_lines[0].get("description") or ""
        if first_desc:
            rev.quote_subject = first_desc[:255]

    extracted_deliv = (ai_result.get("delivery_time") or "").strip()
    if extracted_deliv:
        rev.delivery_time = extracted_deliv[:255]

    line_details = dict(rev.line_details or {})
    for i, ln in enumerate(ai_lines, 1):
        k = str(i)
        cur_ld = dict(line_details.get(k) or {})
        cc = (ln.get("commodity_code") or "").strip()
        notes = (ln.get("notes") or "").strip()
        if cc and not cur_ld.get("commodity_code"):
            cur_ld["commodity_code"] = cc
        if notes and not cur_ld.get("notes"):
            cur_ld["notes"] = notes
        if not cur_ld.get("quote_description"):
            part = (ln.get("part_no") or "").strip()
            desc = (ln.get("description") or "").strip()
            cur_ld["quote_description"] = zoho.build_line_description(
                description=desc,
                part=part,
                spec=notes,
                commodity_code=cc,
            )
        line_details[k] = cur_ld
    rev.line_details = line_details

    rev.snapshot = new_snap
    rev.save(update_fields=["snapshot", "quote_subject", "delivery_time", "line_details", "updated_at"])

    # 7. Record QuoteEvent
    filename = os.path.basename(quote.quote_file.name) if quote.quote_file else "file"
    warnings = list(ai_result.get("warnings") or [])
    if not exchange_rate:
        warnings.append(f"No exchange rate known for {cur} - type the IDR rate in J1")
    msg = f"AI filled {len(sheet_lines)} lines from {filename}"
    if warnings:
        msg += f" (Warnings: {'; '.join(warnings)})"
    event(quote, "ai_filled", msg, user, rev)

    return len(sheet_lines)


# ---------------------------------------------------------------- delete / void / restore

def can_delete_quote(quote, user):
    """
    Allowed only if NONE of its revisions was ever APPROVED or VOID
    (all are DRAFT, SENT_BACK, IN_REVIEW or REJECTED).
    Who: quote's creator (with can_edit) or any approver.
    IN_REVIEW quotes: approvers only.
    """
    if not user.is_authenticated:
        return False
    if quote.revisions.filter(status__in=[QuoteRevision.STATUS_APPROVED, QuoteRevision.STATUS_VOID]).exists():
        return False
    if quote.revisions.filter(status=QuoteRevision.STATUS_IN_REVIEW).exists():
        return can_approve(user)
    return can_approve(user) or (can_edit(user) and quote.created_by_id == user.id)


@transaction.atomic
def delete_quote(quote, user):
    """
    Delete a quote, its uploaded quote_file, revisions, and cascaded objects.
    If the enquiry has no quotes left afterwards, delete the enquiry too.
    """
    if not can_delete_quote(quote, user):
        if quote.revisions.filter(status__in=[QuoteRevision.STATUS_APPROVED, QuoteRevision.STATUS_VOID]).exists():
            raise WorkflowError("Approved or voided quotes cannot be deleted.")
        raise WorkflowError("You don't have permission to delete this quote.")

    latest_rev = quote.latest_revision
    rev_info = f" ({latest_rev.label}, {latest_rev.get_status_display()})" if latest_rev else ""
    desc = f"Deleted vendor quote {quote.enquiry.number} {quote.vendor.name} {quote.vendor_quote_no or ''}{rev_info}".strip()

    event(quote, "deleted", desc, user)
    log_workspace_activity(user, "DELETE", desc)

    if quote.quote_file:
        quote.quote_file.delete(save=False)
    for rev in quote.revisions.all():
        if rev.excel_file:
            rev.excel_file.delete(save=False)

    enquiry = quote.enquiry
    quote.delete()

    enquiry_deleted = False
    if not enquiry.quotes.exists():
        enquiry_desc = f"Deleted enquiry {enquiry.number} ({enquiry.customer_name}) as it has no quotes remaining"
        log_workspace_activity(user, "DELETE", enquiry_desc)
        enquiry.delete()
        enquiry_deleted = True

    return enquiry_deleted


def can_discard_revision(rev, user):
    """
    Only the LATEST revision, only when number > 0 and status is DRAFT or SENT_BACK.
    Who: editors (can_edit(user)). For revision 0, show 'Delete quote' instead.
    """
    if not user.is_authenticated or not can_edit(user):
        return False
    if rev.number <= 0:
        return False
    if rev.status not in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_SENT_BACK):
        return False
    latest = rev.quote.latest_revision
    return latest is not None and latest.id == rev.id


@transaction.atomic
def discard_revision(rev, user):
    """
    Discard (delete) an unapproved latest revision (number > 0). The previous version stays.
    """
    if not can_discard_revision(rev, user):
        if rev.number <= 0:
            raise WorkflowError("The original revision (R0) cannot be discarded. Delete the quote instead.")
        if rev.status not in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_SENT_BACK):
            raise WorkflowError(f"Cannot discard a revision with status {rev.get_status_display()}.")
        latest = rev.quote.latest_revision
        if latest and latest.id != rev.id:
            raise WorkflowError("Only the latest revision can be discarded.")
        raise WorkflowError("You don't have permission to discard this revision.")

    quote = rev.quote
    desc = f"Discarded revision {rev.label} of quote {quote.enquiry.number} {quote.vendor.name}"

    event(quote, "discard_revision", desc, user, revision=None)
    log_workspace_activity(user, "DELETE", desc)

    if rev.excel_file:
        rev.excel_file.delete(save=False)

    rev.delete()
    return quote


def can_void_revision(rev, user):
    """
    Approvers only, only when revision is APPROVED.
    """
    if not user.is_authenticated or not can_approve(user):
        return False
    return rev.status == QuoteRevision.STATUS_APPROVED


@transaction.atomic
def void_revision(rev, user, comment):
    """
    Void an approved revision. Keep record, Excel file, and Zoho number for history.
    """
    rev = QuoteRevision.objects.select_for_update().get(pk=rev.pk)
    if not can_void_revision(rev, user):
        if rev.status != QuoteRevision.STATUS_APPROVED:
            raise WorkflowError(f"Only approved revisions can be voided (current status: {rev.get_status_display()}).")
        raise WorkflowError("Only approvers can void an approved revision.")

    comment = (comment or "").strip()
    if not comment:
        raise WorkflowError("A comment explaining why this revision is being voided is required.")

    quote = rev.quote
    desc = f"Voided revision {rev.label} of quote {quote.enquiry.number} {quote.vendor.name}: {comment}"

    event(quote, "void_revision", desc, user, rev)
    log_workspace_activity(user, "STATUS_CHANGE", desc)

    rev.status = QuoteRevision.STATUS_VOID
    rev.decision_comment = comment
    rev.decided_by = user
    rev.decided_at = timezone.now()
    rev.save(update_fields=["status", "decision_comment", "decided_by", "decided_at", "updated_at"])

    return rev


def can_delete_enquiry(enquiry, user):
    """
    Allowed only if no quote under it has an APPROVED or VOID revision.
    Who: approvers only.
    """
    if hasattr(enquiry, "is_authenticated") and not hasattr(user, "is_authenticated"):
        user, enquiry = enquiry, user
    if not user.is_authenticated or not can_approve(user):
        return False
    return not QuoteRevision.objects.filter(
        quote__enquiry=enquiry,
        status__in=[QuoteRevision.STATUS_APPROVED, QuoteRevision.STATUS_VOID]
    ).exists()


@transaction.atomic
def delete_enquiry(enquiry, user, confirm_number=""):
    """
    Delete an enquiry and all quotes under it. Confirmation must match enquiry number.
    """
    if hasattr(enquiry, "is_authenticated") and not hasattr(user, "is_authenticated"):
        user, enquiry = enquiry, user
    if not can_delete_enquiry(enquiry, user):
        if QuoteRevision.objects.filter(
            quote__enquiry=enquiry,
            status__in=[QuoteRevision.STATUS_APPROVED, QuoteRevision.STATUS_VOID]
        ).exists():
            raise WorkflowError("Cannot delete enquiry: it has approved or voided vendor quotes.")
        raise WorkflowError("Only approvers can delete enquiries.")

    confirm = (confirm_number or "").strip()
    if confirm != enquiry.number.strip():
        raise WorkflowError(f"Confirmation number '{confirm}' does not match '{enquiry.number}'.")

    # Delete quote storage files
    for quote in enquiry.quotes.all():
        desc_q = f"Deleted vendor quote {quote.enquiry.number} {quote.vendor.name} (cascade from enquiry deletion)"
        log_workspace_activity(user, "DELETE", desc_q)
        if quote.quote_file:
            quote.quote_file.delete(save=False)
        for rev in quote.revisions.all():
            if rev.excel_file:
                rev.excel_file.delete(save=False)

    desc = f"Deleted enquiry {enquiry.number} ({enquiry.customer_name})"
    log_workspace_activity(user, "DELETE", desc)

    enquiry.delete()


def can_delete_vendor(vendor, user):
    """
    DELETE allowed only if the vendor has no quotes at all.
    Who: approvers or SUPER_ADMIN/ADMIN.
    """
    if not user.is_authenticated:
        return False
    is_admin = can_approve(user) or getattr(user, "role", None) in ("SUPER_ADMIN", "ADMIN") or getattr(user, "is_superuser", False)
    if not is_admin:
        return False
    return not vendor.quotes.exists()


@transaction.atomic
def delete_vendor(vendor, user):
    """
    Delete a vendor with no quotes, its master files from storage, and MasterSheets.
    """
    if not can_delete_vendor(vendor, user):
        if vendor.quotes.exists():
            raise WorkflowError(f"Cannot delete vendor '{vendor.name}' because it has existing quotes. You can deactivate it instead.")
        raise WorkflowError("Only admins and approvers can delete vendors.")

    desc = f"Deleted vendor {vendor.name} ({vendor.code or 'no code'})"
    log_workspace_activity(user, "DELETE", desc)

    for mf in vendor.master_files.all():
        if mf.file:
            mf.file.delete(save=False)

    vendor.delete()


def toggle_vendor_active(vendor, user, is_active=None):
    """
    Deactivate or activate a vendor.
    """
    is_admin = can_approve(user) or getattr(user, "role", None) in ("SUPER_ADMIN", "ADMIN") or getattr(user, "is_superuser", False)
    if not is_admin:
        raise WorkflowError("Only admins and approvers can change vendor active status.")

    if is_active is None:
        is_active = not vendor.is_active

    vendor.is_active = is_active
    vendor.save(update_fields=["is_active"])

    action = "ACTIVATE" if is_active else "DEACTIVATE"
    desc = f"{'Activated' if is_active else 'Deactivated'} vendor {vendor.name} ({vendor.code or ''})"
    log_workspace_activity(user, action, desc)
    return vendor


def can_delete_master_file(mf, user):
    """
    Approvers only. Can delete any version except the current (newest) one.
    """
    if not user.is_authenticated or not can_approve(user):
        return False
    current = mf.vendor.current_master
    return current is None or current.id != mf.id


def can_restore_master_file(mf, user):
    """
    Approvers only. Can restore any version except the current (newest) one.
    """
    if not user.is_authenticated or not can_approve(user):
        return False
    current = mf.vendor.current_master
    return current is not None and current.id != mf.id


@transaction.atomic
def delete_master_file(mf, user):
    """
    Delete an older master file version and its file from storage.
    """
    if not can_delete_master_file(mf, user):
        current = mf.vendor.current_master
        if current and current.id == mf.id:
            raise WorkflowError("Cannot delete the current active master file.")
        raise WorkflowError("Only approvers can delete master file versions.")

    desc = f"Deleted master file version {mf.filename} ({mf.created_at.strftime('%Y-%m-%d %H:%M')}) of vendor {mf.vendor.name}"
    log_workspace_activity(user, "DELETE", desc)

    if mf.file:
        mf.file.delete(save=False)
    mf.delete()


@transaction.atomic
def restore_master_file(mf, user):
    """
    Restore an older master file version by copying it as the newest VendorMasterFile.
    """
    if not can_restore_master_file(mf, user):
        current = mf.vendor.current_master
        if current and current.id == mf.id:
            raise WorkflowError("This version is already the current master file.")
        raise WorkflowError("Only approvers can restore master file versions.")

    mf.file.open("rb")
    try:
        content = mf.file.read()
    finally:
        mf.file.close()

    restored = VendorMasterFile(
        vendor=mf.vendor,
        sheet_count=mf.sheet_count,
        note=f"Restored from {mf.created_at.strftime('%b %d, %Y %H:%M')}",
        uploaded_by=user,
    )
    restored.file.save(mf.filename, ContentFile(content), save=True)

    desc = f"Restored master file version from {mf.created_at.strftime('%Y-%m-%d %H:%M')} for vendor {mf.vendor.name}"
    log_workspace_activity(user, "UPDATE", desc)

    return restored


def can_delete_master_sheet(sheet, user):
    """
    Allowed for editors (can_edit(user)).
    """
    return user.is_authenticated and can_edit(user)


@transaction.atomic
def delete_master_sheet(sheet, user):
    """
    Delete a single MasterSheet record (never edits the Excel master file).
    """
    if not can_delete_master_sheet(sheet, user):
        raise WorkflowError("You don't have permission to delete past quote sheets.")

    desc = f"Deleted past quote sheet '{sheet.sheet_name}' ({sheet.enquiry_no or 'no ENQ'}) of vendor {sheet.vendor.name}"
    log_workspace_activity(user, "DELETE", desc)

    vendor_id = sheet.vendor_id
    sheet.delete()
    return vendor_id


@transaction.atomic
def bulk_delete_master_sheets(vendor, sheet_ids, user):
    """
    Bulk delete MasterSheet records by IDs for a vendor.
    """
    if not user.is_authenticated or not can_edit(user):
        raise WorkflowError("You don't have permission to delete past quote sheets.")

    sheets = vendor.master_sheets.filter(id__in=sheet_ids)
    count = sheets.count()
    if count == 0:
        return 0

    desc = f"Deleted {count} past quote sheet(s) from vendor {vendor.name}"
    log_workspace_activity(user, "DELETE", desc)

    sheets.delete()
    return count
