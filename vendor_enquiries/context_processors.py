from django.utils.functional import SimpleLazyObject


def vendor_enquiries(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    from . import services
    return {"can_use_vendor_enquiries": SimpleLazyObject(lambda: services.can_use(user))}
