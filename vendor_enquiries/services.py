"""Access rules, actions and emails for the Vendor Enquiry Tracker."""
import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import AccessChange, QuoteFile, Remark, TrackerAccess, TrackerLog, VendorEnquiry

logger = logging.getLogger(__name__)
User = get_user_model()

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")
ALLOWED_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg")
MAX_UPLOAD_MB = 25

LEVEL_ADMIN = "ADMIN"


class TrackerError(Exception):
    pass


# ---------------------------------------------------------------- access

def is_admin(user):
    return bool(user and user.is_authenticated and
                (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES))


def level(user):
    """ADMIN, MANAGER, USER or None (no access). Cached on the user object."""
    if not user or not user.is_authenticated:
        return None
    if not hasattr(user, "_ve_level"):
        if is_admin(user):
            user._ve_level = LEVEL_ADMIN
        else:
            row = TrackerAccess.objects.filter(user=user).only("level").first()
            user._ve_level = row.level if row else None
    return user._ve_level


def can_use(user):
    return level(user) is not None


def is_manager(user):
    return level(user) in (LEVEL_ADMIN, TrackerAccess.LEVEL_MANAGER)


def can_see(user, enq):
    return is_manager(user) or (can_use(user) and enq.created_by_id == user.id)


def can_edit(user, enq):
    """The person who made the row, or an admin."""
    return can_use(user) and (enq.created_by_id == user.id or is_admin(user))


def can_remark(user, enq):
    return is_manager(user)


def can_reply(user, enq):
    return can_see(user, enq)


def can_send_to_pricing(user, enq):
    from vendor_quotes.services import workflow
    return is_manager(user) and workflow.can_edit(user)


def visible_qs(user):
    qs = VendorEnquiry.objects.select_related("created_by", "vendor")
    if is_manager(user):
        return qs
    if can_use(user):
        return qs.filter(created_by=user)
    return qs.none()


@transaction.atomic
def set_access(target, new_level, by):
    new_level = new_level or ""
    if new_level not in ("", TrackerAccess.LEVEL_USER, TrackerAccess.LEVEL_MANAGER):
        raise TrackerError("Unknown access level.")
    if is_admin(target):
        raise TrackerError(f"{target.full_name or target.email} is an Admin and always has full access.")
    row = TrackerAccess.objects.filter(user=target).first()
    old = row.level if row else ""
    if old == new_level:
        return False
    if new_level:
        TrackerAccess.objects.update_or_create(user=target, defaults={"level": new_level, "granted_by": by})
    elif row:
        row.delete()
    AccessChange.objects.create(user=target, changed_by=by, old_level=old, new_level=new_level)
    _activity(by, "UPDATE", f"Vendor Enquiry Tracker access for {target.full_name or target.email}: "
                            f"{old or 'No access'} -> {new_level or 'No access'}")
    return True


# ---------------------------------------------------------------- helpers

def log(enq, action, message, user):
    TrackerLog.objects.create(enquiry=enq, label=enq.label if enq else "", user=user, action=action, message=message)


def _activity(user, action, message):
    try:
        from vendor_quotes.services.workflow import log_workspace_activity
        log_workspace_activity(user, action, message)
    except Exception as e:  # never block the tracker on the activity log
        logger.warning("activity log failed: %s", e)


def validate_upload(f):
    if not f:
        return
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise TrackerError("Please upload the vendor quote as PDF, Excel or an image (JPG/PNG).")
    if f.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise TrackerError(f"The file is larger than {MAX_UPLOAD_MB} MB.")


def match_vendor(name):
    from vendor_quotes.models import Vendor
    name = (name or "").strip()
    return Vendor.objects.filter(name__iexact=name).first() if name else None


def vendor_names():
    from vendor_quotes.models import Vendor
    return list(Vendor.objects.filter(is_active=True).order_by("name").values_list("name", flat=True))


def _name(user):
    return (user.full_name or user.email) if user else "Someone"


# ---------------------------------------------------------------- emails

def _site():
    return getattr(settings, "SITE_URL", "https://flowforceworkspace.cloud").rstrip("/")


def upload_recipients():
    return list(getattr(settings, "VENDOR_ENQUIRY_UPLOAD_EMAILS", ["umapathi@flow-force.com"]))


def remark_recipients():
    return list(getattr(settings, "VENDOR_ENQUIRY_REMARK_EMAILS", ["automation@flow-force.com"]))


def _send(recipients, subject, title, enq, intro, query="", extra_rows=None):
    from tasks.models import EmailLog, Notification
    from tasks.tasks import send_email_log_task
    link = reverse("vendor_enquiries:detail", args=[enq.id])
    html = render_to_string("vendor_enquiries/email.html", {
        "title": title, "intro": intro, "enq": enq, "query": query, "url": _site() + link,
        "extra_rows": extra_rows or [],
    })
    for email in dict.fromkeys(e.strip().lower() for e in recipients if e and e.strip()):
        log_row = EmailLog.objects.create(recipient_email=email, subject=f"[Flow-Force] {subject}", body=html,
                                          email_type="VENDOR_QUOTE_MAIL", status="PENDING", max_retries=3)
        transaction.on_commit(lambda log_id=log_row.id: send_email_log_task.delay(log_id))
        for u in User.objects.filter(email__iexact=email, is_active=True):
            Notification.objects.create(user=u, title=title, description=intro, type="SYSTEM", link=link)


def email_quote_uploaded(enq, qf, user):
    if qf.revision:
        title = f"Revised quote uploaded - Revision {qf.revision}"
        intro = f"{_name(user)} uploaded Revision {qf.revision} of the vendor quote."
    else:
        title = "Vendor quote uploaded"
        intro = f"{_name(user)} uploaded the vendor quote."
    _send(upload_recipients(), f"{title} - {enq.enquiry_no} - {enq.vendor_name}", title, enq, intro,
          extra_rows=[("Quote file", f"{qf.label}: {qf.filename}")])


def email_remark(enq, remark, user):
    title = "Query on vendor quote"
    intro = f"{_name(user)} added a query on this vendor quote. Kindly check with the vendor."
    _send(remark_recipients(), f"Query on vendor quote - {enq.enquiry_no} - {enq.vendor_name}", title, enq,
          intro, query=remark.text.upper())


# ---------------------------------------------------------------- actions

def _add_file(enq, f, user, vendor_quote_no=""):
    last = enq.files.order_by("-revision").first()
    qf = QuoteFile(enquiry=enq, revision=(last.revision + 1) if last else 0, uploaded_by=user,
                   vendor_quote_no=vendor_quote_no or enq.vendor_quote_no)
    qf.file.save(os.path.basename(f.name), f, save=True)
    return qf


@transaction.atomic
def save_enquiry(enq, data, user, upload=None):
    """Create or update a row. data: enquiry_no, customer_name, vendor_name, initial_enquiry, sent_on,
    status, vendor_quote_no. Returns (enq, warnings)."""
    validate_upload(upload)
    creating = enq.pk is None
    old_status = None if creating else VendorEnquiry.objects.get(pk=enq.pk).status
    enq.enquiry_no = data["enquiry_no"].strip().upper()
    enq.customer_name = data.get("customer_name", "").strip()
    enq.vendor_name = data["vendor_name"].strip()
    enq.vendor = match_vendor(enq.vendor_name)
    if enq.vendor:
        enq.vendor_name = enq.vendor.name
    enq.initial_enquiry = data.get("initial_enquiry", "").strip()
    if data.get("sent_on"):
        enq.sent_on = data["sent_on"]
    enq.vendor_quote_no = data.get("vendor_quote_no", "").strip()
    status = data.get("status") or VendorEnquiry.STATUS_SENT
    if upload:
        status = VendorEnquiry.STATUS_QUOTE_GOT   # a quote in hand means the quote is got
    has_file = (not creating) and enq.files.exists()
    if status == VendorEnquiry.STATUS_QUOTE_GOT and not (upload or has_file):
        raise TrackerError("Please upload the vendor quote to set the status to Quote Got.")
    enq.status = status
    if status == VendorEnquiry.STATUS_QUOTE_GOT and not enq.quote_got_at:
        enq.quote_got_at = timezone.now()
    if creating:
        enq.created_by = user
    enq.save()

    warnings = []
    if not enq.vendor:
        warnings.append(f'Vendor "{enq.vendor_name}" not found in Vendor Quotes, kindly create it.')

    if creating:
        log(enq, "created", f"Enquiry added ({enq.get_status_display()})", user)
    else:
        changes = f"Status: {dict(VendorEnquiry.STATUS_CHOICES).get(old_status)} -> {enq.get_status_display()}" \
            if old_status != enq.status else "Details updated"
        log(enq, "updated", changes, user)

    if upload:
        qf = _add_file(enq, upload, user)
        log(enq, "uploaded", f"{qf.label} uploaded: {qf.filename}", user)
        email_quote_uploaded(enq, qf, user)
    return enq, warnings


@transaction.atomic
def revise(enq, upload, user, vendor_quote_no=""):
    if not upload:
        raise TrackerError("Please choose the revised quote file.")
    validate_upload(upload)
    if not enq.files.exists():
        raise TrackerError("Upload the first vendor quote before adding a revision.")
    if vendor_quote_no.strip():
        enq.vendor_quote_no = vendor_quote_no.strip()
    enq.status = VendorEnquiry.STATUS_QUOTE_GOT
    enq.save()
    qf = _add_file(enq, upload, user, vendor_quote_no=enq.vendor_quote_no)
    log(enq, "revised", f"{qf.label} uploaded: {qf.filename}", user)
    email_quote_uploaded(enq, qf, user)
    return qf


@transaction.atomic
def delete(enq, user):
    label = enq.label
    names = [qf.file.name for qf in enq.files.all()]
    TrackerLog.objects.create(enquiry=None, label=label, user=user, action="deleted",
                              message=f"Enquiry row deleted ({enq.get_status_display()}, {len(names)} file(s))")
    _activity(user, "DELETE", f"Deleted Vendor Enquiry Tracker row {label}")
    files = [qf.file for qf in enq.files.all()]
    enq.delete()

    def _remove():
        for f in files:
            try:
                f.storage.delete(f.name)
            except Exception as e:
                logger.warning("Could not delete %s: %s", f.name, e)
    transaction.on_commit(_remove)


@transaction.atomic
def add_remark(enq, text, user):
    text = (text or "").strip()
    if not text:
        raise TrackerError("Please type the remark.")
    r = Remark.objects.create(enquiry=enq, author=user, text=text)
    log(enq, "remark", "Remark added", user)
    VendorEnquiry.objects.filter(pk=enq.pk).update(updated_at=timezone.now())
    email_remark(enq, r, user)
    return r


@transaction.atomic
def add_reply(enq, remark, text, user):
    text = (text or "").strip()
    if not text:
        raise TrackerError("Please type the reply.")
    if remark.enquiry_id != enq.id or remark.parent_id:
        raise TrackerError("Reply to a remark of this enquiry.")
    r = Remark.objects.create(enquiry=enq, parent=remark, author=user, text=text)
    log(enq, "reply", "Reply added", user)
    VendorEnquiry.objects.filter(pk=enq.pk).update(updated_at=timezone.now())
    return r


@transaction.atomic
def send_to_pricing(enq, user, create_vendor=False):
    """Create the Vendor Quote (PDF attached) for price calculation, or attach a newer revision to it.
    Returns (vendor_quote, message)."""
    from vendor_quotes.models import Vendor, VendorQuote
    from vendor_quotes.services import workflow

    enq = VendorEnquiry.objects.select_for_update().get(pk=enq.pk)
    qf = enq.latest_file
    if enq.status != VendorEnquiry.STATUS_QUOTE_GOT or not qf:
        raise TrackerError("The vendor quote must be uploaded (status Quote Got) first.")

    vendor = enq.vendor or match_vendor(enq.vendor_name)
    if not vendor:
        if not create_vendor:
            raise TrackerError("VENDOR_MISSING")
        vendor = Vendor.objects.create(name=enq.vendor_name.strip())
        log(enq, "vendor_created", f'Vendor "{vendor.name}" created in Vendor Quotes (name only)', user)
        _activity(user, "CREATE", f'Created vendor "{vendor.name}" from the Vendor Enquiry Tracker')
    enq.vendor = vendor

    with qf.file.open("rb") as fh:
        content = ContentFile(fh.read(), name=qf.filename)

    quote = enq.vendor_quote if enq.vendor_quote_id and VendorQuote.objects.filter(pk=enq.vendor_quote_id).exists() else None
    if quote is None:
        quote, rev = workflow.create_quote(
            enquiry_number=enq.enquiry_no, customer_name=enq.customer_name or "-", vendor=vendor,
            vendor_quote_no=qf.vendor_quote_no or enq.vendor_quote_no, quote_file=content, user=user)
        workflow.event(quote, "from_tracker", f"Sent from the Vendor Enquiry Tracker ({qf.label})", user, rev)
        msg = "Sent to Price Calculation. Click Read with AI to fill the price sheet."
    else:
        if enq.sent_file_id == qf.id:
            raise TrackerError("This quote file is already in Price Calculation.")
        quote.quote_file.save(qf.filename, content, save=False)
        if qf.vendor_quote_no or enq.vendor_quote_no:
            quote.vendor_quote_no = qf.vendor_quote_no or enq.vendor_quote_no
        quote.save(update_fields=["quote_file", "vendor_quote_no", "updated_at"])
        workflow.event(quote, "from_tracker", f"{qf.label} of the vendor quote attached from the Vendor Enquiry Tracker", user)
        msg = f"{qf.label} attached to the Price Calculation. Click Read with AI again to update the sheet."

    enq.vendor_quote = quote
    enq.sent_file = qf
    enq.sent_to_pricing_at = timezone.now()
    enq.sent_to_pricing_by = user
    enq.save()
    log(enq, "sent_to_pricing", f"{qf.label} sent to Price Calculation", user)
    return quote, msg
