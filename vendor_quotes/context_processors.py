from django.utils.functional import SimpleLazyObject


def vendor_quotes(request):
    """Sidebar flags for Vendor Quotes. Lazy, and role-based first, so most pages add no queries."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    from .services import workflow

    def waiting():
        # Badge only for approver roles (group-based approvers still see the queue on the Vendor Quotes page).
        if getattr(user, "role", None) not in workflow.approver_roles():
            return 0
        from .models import QuoteRevision
        return QuoteRevision.objects.filter(status=QuoteRevision.STATUS_IN_REVIEW).count()

    return {
        "can_use_vendor_quotes": SimpleLazyObject(lambda: workflow.can_edit(user)),
        "vendor_quotes_waiting": SimpleLazyObject(waiting),
    }
