"""
Context processor for Tickets module.
Injects lazy counts for templates that specifically reference them.
"""
from django.utils.functional import SimpleLazyObject


def tickets_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    def get_open_count():
        from .models import Ticket
        return Ticket.objects.filter(
            is_deleted=False
        ).exclude(
            status__in=[Ticket.STATUS_CLOSED, Ticket.STATUS_COMPLETED]
        ).count()

    def get_expiring_count():
        from django.utils import timezone
        from .models import Ticket
        today = timezone.localdate()
        in_30_days = today + timezone.timedelta(days=30)
        return Ticket.objects.filter(
            is_deleted=False,
            warranty_end__gte=today,
            warranty_end__lte=in_30_days
        ).count()

    return {
        "tickets_open_count": SimpleLazyObject(get_open_count),
        "tickets_expiring_warranties_count": SimpleLazyObject(get_expiring_count),
    }
