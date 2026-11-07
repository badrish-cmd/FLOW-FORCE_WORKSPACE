from celery import shared_task

from .models import QuoteRevision
from .services import workflow


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
