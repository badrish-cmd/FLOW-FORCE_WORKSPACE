import logging
import os

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import QuoteRevision, VendorQuote
from .services import ai_extract, workflow

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task
def run_approval_outputs(revision_id, excel=True, zoho_draft=True):
    """After approval: write the Excel price table into the vendor master, then create the Zoho draft."""
    try:
        rev = QuoteRevision.objects.select_related("quote__vendor", "quote__enquiry").get(pk=revision_id)
    except QuoteRevision.DoesNotExist:
        return
    workflow.run_outputs(rev, excel=excel, zoho_draft=zoho_draft)


@shared_task
def retry_waiting_zoho_drafts():
    """Once Zoho is connected, create drafts for approvals that were waiting for the connection."""
    for rev in QuoteRevision.objects.filter(zoho_status=QuoteRevision.OUTPUT_WAITING).order_by("quote_id", "number"):
        workflow.run_outputs(rev, excel=False, zoho_draft=True)


@shared_task
def read_vendor_quote_with_ai(quote_id, user_id):
    """
    Celery task: Extract line items from vendor quote file using Gemini AI,
    and apply the extracted lines to the latest editable revision.
    """
    try:
        quote = VendorQuote.objects.select_related("vendor", "enquiry").get(pk=quote_id)
    except VendorQuote.DoesNotExist:
        logger.error("read_vendor_quote_with_ai: VendorQuote %s not found", quote_id)
        return

    user = User.objects.filter(pk=user_id).first() if user_id else None
    rev = quote.latest_revision

    if not rev or rev.status not in (QuoteRevision.STATUS_DRAFT, QuoteRevision.STATUS_SENT_BACK):
        quote.ai_status = VendorQuote.AI_FAILED
        quote.ai_error = "The latest revision is not editable (must be Draft or Sent back)."
        quote.save(update_fields=["ai_status", "ai_error", "updated_at"])
        return

    if user and not workflow.can_edit_revision(user, rev):
        quote.ai_status = VendorQuote.AI_FAILED
        quote.ai_error = "User does not have permission to edit this revision."
        quote.save(update_fields=["ai_status", "ai_error", "updated_at"])
        return

    if not quote.quote_file:
        quote.ai_status = VendorQuote.AI_FAILED
        quote.ai_error = "No vendor quote file is uploaded."
        quote.save(update_fields=["ai_status", "ai_error", "updated_at"])
        return

    quote.ai_status = VendorQuote.AI_RUNNING
    quote.ai_error = ""
    quote.save(update_fields=["ai_status", "ai_error", "updated_at"])

    try:
        quote.quote_file.open("rb")
        try:
            file_bytes = quote.quote_file.read()
        finally:
            quote.quote_file.close()

        filename = os.path.basename(quote.quote_file.name)
        result = ai_extract.extract_quote(file_bytes, filename)
    except ai_extract.AIExtractError as e:
        quote.ai_status = VendorQuote.AI_FAILED
        quote.ai_error = str(e)
        quote.save(update_fields=["ai_status", "ai_error", "updated_at"])
        workflow.event(quote, "ai_failed", f"AI extraction failed: {e}", user, rev)
        return
    except Exception as e:
        logger.exception("AI reading failed unexpectedly for quote %s", quote_id)
        quote.ai_status = VendorQuote.AI_FAILED
        quote.ai_error = f"Unexpected error: {e}"
        quote.save(update_fields=["ai_status", "ai_error", "updated_at"])
        workflow.event(quote, "ai_failed", f"AI extraction failed: {e}", user, rev)
        return

    # Success: Save result and apply to revision
    quote.ai_status = VendorQuote.AI_DONE
    quote.ai_result = result
    quote.ai_error = ""
    quote.ai_ran_at = timezone.now()
    quote.save(update_fields=["ai_status", "ai_result", "ai_error", "ai_ran_at", "updated_at"])

    try:
        workflow.apply_ai_result(quote, rev, result, user)
    except Exception as e:
        logger.exception("Failed to apply AI result to revision %s", rev.id)
        quote.ai_status = VendorQuote.AI_FAILED
        quote.ai_error = f"Extracted successfully but failed to apply to sheet: {e}"
        quote.save(update_fields=["ai_status", "ai_error", "updated_at"])
        workflow.event(quote, "ai_failed", f"Failed applying AI lines: {e}", user, rev)

