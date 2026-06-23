from django.contrib.auth.decorators import login_required

from .models import Notification
from .services import get_visible_trackers


def task_notifications(request):
    if not request.user.is_authenticated:
        return {}

    notifications = (
        Notification.objects.filter(user=request.user)
        .select_related("row", "row__tracker")
        .order_by("-sent_at", "-id")[:8]
    )
    unread_count = Notification.objects.filter(user=request.user, read=False).count()
    visible_trackers = get_visible_trackers(request.user)
    return {
        "task_notifications": notifications,
        "task_notifications_unread": unread_count,
        "sidebar_trackers": visible_trackers,
    }
