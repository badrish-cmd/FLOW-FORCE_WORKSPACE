from celery import shared_task


@shared_task
def refresh_deal_owners(ids=None, limit=60):
    """Fill deal owners from Zoho CRM for the given follow-ups, or for those still missing one."""
    from . import services
    from .models import SalesFollowup
    qs = SalesFollowup.objects.filter(pk__in=ids) if ids else services.needs_owner_qs()
    found = 0
    for fu in qs[:limit]:
        if services.refresh_owner(fu):
            found += 1
    return found
