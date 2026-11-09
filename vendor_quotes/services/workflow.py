"""Business actions for vendor quotes: create, save, submit, approve, send back, reject, revise."""
import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..models import (Enquiry, MasterSheet, QuoteChange, QuoteEvent, QuoteRevision, Vendor,
                      VendorMasterFile, VendorQuote)
from . import zoho
from .excel_io import append_to_master, build_single_workbook, import_master
from .sheet import (build_standard_snapshot, cell_number, cell_text, diff_snapshots,
                    extract_lines, first_sheet, fresh_copy, get_cell)

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


def notify(users, title, message, quote, kind="SYSTEM"):
    """Workspace bell + email (sent from DEFAULT_FROM_EMAIL via the existing EmailLog queue)."""
    from tasks.models import EmailLog, Notification
    from tasks.tasks import send_email_log_task
    link = reverse("vendor_quotes:quote_detail", args=[quote.id])
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


def event(quote, action, message="", user=None, revision=None):
    QuoteEvent.objects.create(quote=quote, revision=revision, user=user, action=action, message=message)


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
                                       currency=(currency or vendor.currency), created_by=user)
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
            exchange_rate=vendor.default_exchange_rate, margin_factor=vendor.default_margin_factor,
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
    if latest.status != QuoteRevision.STATUS_APPROVED:
        raise WorkflowError("You can only revise the latest approved version.")
    rev = QuoteRevision.objects.create(quote=quote, number=latest.number + 1,
                                       snapshot=fresh_copy(latest.snapshot), created_by=user)
    event(quote, "revised", f"{rev.label} started from {latest.label}", user, rev)
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
        est_id, est_no = zoho.create_draft_estimate(
            customer_id=customer_id, reference=q.enquiry.number, lines=lines,
            estimate_number=estimate_number)
        rev.zoho_estimate_id, rev.zoho_estimate_number = est_id, est_no or (estimate_number or "")
        rev.zoho_status = QuoteRevision.OUTPUT_DONE
        rev.zoho_error = ""
        event(q, "zoho_done", f"Draft quotation {rev.zoho_estimate_number} created in Zoho Books", None, rev)
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

    # 2. Update quote currency if empty
    extracted_cur = (ai_result.get("currency") or "").strip().upper()
    if extracted_cur and not quote.currency:
        quote.currency = extracted_cur
        quote.save(update_fields=["currency", "updated_at"])

    # 3. Read existing parameters and factor values from current snapshot to preserve user's factors
    old_snap = rev.snapshot or {}
    sh = first_sheet(old_snap)

    exchange_rate = quote.vendor.default_exchange_rate
    margin_factor = quote.vendor.default_margin_factor
    duty_factor = quote.vendor.default_duty_factor
    freight_factor = quote.vendor.default_freight_factor
    freight = 0
    quote_total = ""

    if sh:
        c_rate = cell_number(get_cell(sh, 0, 9))
        if c_rate is not None and c_rate > 0:
            exchange_rate = c_rate
        c_margin = cell_number(get_cell(sh, 1, 9))
        if c_margin is not None and c_margin > 0:
            margin_factor = c_margin
        c_duty = cell_number(get_cell(sh, 2, 9))
        if c_duty is not None and c_duty > 0:
            duty_factor = c_duty
        c_ffactor = cell_number(get_cell(sh, 3, 9))
        if c_ffactor is not None and c_ffactor > 0:
            freight_factor = c_ffactor
        c_freight = cell_number(get_cell(sh, 4, 9))
        if c_freight is not None:
            freight = c_freight
        c_total = cell_number(get_cell(sh, 5, 9))
        if c_total is not None:
            quote_total = c_total

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
    cur = quote.currency or quote.vendor.currency or "EUR"

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

    rev.snapshot = new_snap
    rev.save(update_fields=["snapshot", "updated_at"])

    # 6. Record QuoteEvent
    filename = os.path.basename(quote.quote_file.name) if quote.quote_file else "file"
    warnings = ai_result.get("warnings") or []
    msg = f"AI filled {len(sheet_lines)} lines from {filename}"
    if warnings:
        msg += f" (Warnings: {'; '.join(warnings)})"
    event(quote, "ai_filled", msg, user, rev)

    return len(sheet_lines)
