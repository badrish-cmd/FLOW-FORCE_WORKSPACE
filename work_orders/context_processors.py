from django.utils.functional import SimpleLazyObject


def work_orders(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    from . import services
    return {"can_use_work_orders": SimpleLazyObject(lambda: services.can_use(user))}
