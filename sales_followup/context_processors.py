from django.utils.functional import SimpleLazyObject


def sales_followup(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    from . import services
    return {"can_use_sales_followup": SimpleLazyObject(lambda: services.can_use(user))}
