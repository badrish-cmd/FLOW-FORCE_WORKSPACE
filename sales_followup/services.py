"""Access, syncing approved quotes and follow-up entries."""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import FollowupAccess, FollowupAccessChange, FollowupEntry, FollowupLog, SalesFollowup

logger = logging.getLogger(__name__)

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")
LEVEL_ADMIN = "ADMIN"


class FollowupError(Exception):
    pass


# ---------------------------------------------------------------- access

def is_admin(user):
    return bool(user and user.is_authenticated and
                (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES))


def level(user):
    if not user or not user.is_authenticated:
        return None
    if not hasattr(user, "_sf_level"):
        if is_admin(user):
            user._sf_level = LEVEL_ADMIN
        else:
            row = FollowupAccess.objects.filter(user=user).only("level").first()
            user._sf_level = row.level if row else None
    return user._sf_level


def can_use(user):
    return level(user) is not None


def is_manager(user):
    return level(user) in (LEVEL_ADMIN, FollowupAccess.LEVEL_MANAGER)


def can_change_entry(user, entry):
    return is_manager(user) or (can_use(user) and entry.created_by_id == user.id)


@transaction.atomic
def set_access(target, new_level, by):
    new_level = new_level or ""
    if new_level not in ("", FollowupAccess.LEVEL_USER, FollowupAccess.LEVEL_MANAGER):
        raise FollowupError("Unknown access level.")
    if is_admin(target):
        raise FollowupError(f"{target.full_name or target.email} is an Admin and always has full access.")
    row = FollowupAccess.objects.filter(user=target).first()
    old = row.level if row else ""
    if old == new_level:
        return False
    if new_level:
        FollowupAccess.objects.update_or_create(user=target, defaults={"level": new_level, "granted_by": by})
    elif row:
        row.delete()
    FollowupAccessChange.objects.create(user=target, changed_by=by, old_level=old, new_level=new_level)
    return True


def log(fu, message, user):
    FollowupLog.objects.create(followup=fu, label=str(fu) if fu else "", user=user, message=message)


# ---------------------------------------------------------------- approved quotes -> follow-up rows

def _apply(quote, revs):
    """Create/refresh the follow-up row of one quote. revs: its revisions, newest first."""
    from vendor_quotes.models import QuoteRevision
    approved = next((r for r in revs if r.status == QuoteRevision.STATUS_APPROVED), None)
    voided = next((r for r in revs if r.status == QuoteRevision.STATUS_VOID), None)
    if not approved and not voided:
        return None
    rev = approved or voided
    fu = SalesFollowup.objects.filter(quote=quote).first()
    created = fu is None
    if created:
        fu = SalesFollowup(quote=quote)
    fu.revision = rev
    fu.enquiry_no = quote.enquiry.number
    fu.customer_name = quote.enquiry.customer_name
    fu.vendor_name = quote.vendor.name
    fu.revision_label = rev.label
    fu.zoho_number = rev.zoho_estimate_number or fu.zoho_number
    fu.approved_at = rev.decided_at or rev.updated_at
    if approved:
        if fu.status == SalesFollowup.STATUS_CANCELLED:
            fu.status = SalesFollowup.STATUS_WAITING          # voided, then revised and approved again
        if created and not fu.next_followup_date:
            fu.next_followup_date = timezone.localtime(fu.approved_at).date() + timedelta(days=1)
    else:
        fu.status = SalesFollowup.STATUS_CANCELLED
    fu.save()
    return fu


def sync_quote(quote_id):
    from vendor_quotes.models import VendorQuote
    quote = VendorQuote.objects.select_related("enquiry", "vendor").filter(pk=quote_id).first()
    if not quote:
        return None
    return _apply(quote, list(quote.revisions.order_by("-number")))


def sync_all():
    """Bring every approved (or voided) quote into the follow-up list. Cheap enough to run on page load."""
    from django.db.models import Prefetch
    from vendor_quotes.models import QuoteRevision, VendorQuote
    revs = QuoteRevision.objects.order_by("-number").only(
        "id", "quote_id", "number", "status", "decided_at", "updated_at", "zoho_estimate_number")
    quotes = (VendorQuote.objects.filter(revisions__status__in=[QuoteRevision.STATUS_APPROVED, QuoteRevision.STATUS_VOID])
              .distinct().select_related("enquiry", "vendor").prefetch_related(Prefetch("revisions", queryset=revs)))
    existing = {f.quote_id: f for f in SalesFollowup.objects.all()}
    changed = 0
    for q in quotes:
        rs = list(q.revisions.all())
        fu = existing.get(q.id)
        if fu:
            from vendor_quotes.models import QuoteRevision as QR
            approved = next((r for r in rs if r.status == QR.STATUS_APPROVED), None)
            want_rev = approved or next((r for r in rs if r.status == QR.STATUS_VOID), None)
            want_status_cancel = approved is None
            same = (fu.revision_id == (want_rev.id if want_rev else None)
                    and fu.zoho_number == (want_rev.zoho_estimate_number or fu.zoho_number)
                    and fu.enquiry_no == q.enquiry.number and fu.customer_name == q.enquiry.customer_name
                    and (fu.status == SalesFollowup.STATUS_CANCELLED) == want_status_cancel)
            if same:
                continue
        _apply(q, rs)
        changed += 1
    return changed


# ---------------------------------------------------------------- entries

def recompute(fu):
    fu.refresh_from_db()
    entries = list(fu.entries.order_by("-date", "-created_at"))
    fu.followup_count = len(entries)
    if entries:
        last = entries[0]
        fu.last_followup_date = last.date
        if fu.status != SalesFollowup.STATUS_CANCELLED:
            fu.status = last.status
        fu.next_followup_date = last.next_date or (last.date + timedelta(days=1))
    else:
        fu.last_followup_date = None
        if fu.status != SalesFollowup.STATUS_CANCELLED:
            fu.status = SalesFollowup.STATUS_WAITING
        base = timezone.localtime(fu.approved_at).date() if fu.approved_at else timezone.localdate()
        fu.next_followup_date = base + timedelta(days=1)
    fu.save(update_fields=["followup_count", "last_followup_date", "status", "next_followup_date", "updated_at"])
    return fu


def _clean(data):
    contacted = (data.get("contacted") or "").strip()
    if not contacted:
        raise FollowupError("Please write who you followed up with.")
    status = data.get("status") or SalesFollowup.STATUS_FOLLOWED_UP
    if status not in dict(SalesFollowup.ENTRY_STATUSES):
        raise FollowupError("Pick a status.")
    date = data.get("date") or timezone.localdate()
    next_date = data.get("next_date") or None
    if status in (SalesFollowup.STATUS_WON, SalesFollowup.STATUS_LOST):
        next_date = None
    elif not next_date:
        next_date = date + timedelta(days=1)          # not given -> next day
    elif next_date < date:
        raise FollowupError("The next follow-up date can't be before the follow-up date.")
    return {"date": date, "contacted": contacted[:255], "channel": data.get("channel") or "EMAIL",
            "update_text": (data.get("update_text") or "").strip(), "status": status, "next_date": next_date}


@transaction.atomic
def add_entry(fu, data, user):
    if fu.status == SalesFollowup.STATUS_CANCELLED:
        raise FollowupError("This quote was voided - no follow-up needed.")
    e = FollowupEntry.objects.create(followup=fu, created_by=user, **_clean(data))
    log(fu, f"Follow-up recorded ({e.get_status_display()})", user)
    recompute(fu)
    return e


@transaction.atomic
def edit_entry(entry, data, user):
    if not can_change_entry(user, entry):
        raise FollowupError("You can only change your own entries.")
    for k, v in _clean(data).items():
        setattr(entry, k, v)
    entry.edited_at = timezone.now()
    entry.save()
    log(entry.followup, "Follow-up entry edited", user)
    recompute(entry.followup)
    return entry


@transaction.atomic
def delete_entry(entry, user):
    if not can_change_entry(user, entry):
        raise FollowupError("You can only delete your own entries.")
    fu = entry.followup
    log(fu, f"Follow-up entry of {entry.date:%d %b %Y} deleted", user)
    entry.delete()
    recompute(fu)
