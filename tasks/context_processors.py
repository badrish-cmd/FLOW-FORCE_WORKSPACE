from tasks.models import Notification as TasksNotification
from tables.permissions import get_accessible_tables
from django.utils import timezone

def global_context(request):
    if not request.user.is_authenticated:
        return {}

    # Fast count queries for badges
    unread_count = TasksNotification.objects.filter(user=request.user, is_read=False).count()

    # Limit lists to 15 items in database query to prevent loading entire history into memory
    tasks_unread = TasksNotification.objects.filter(user=request.user, is_read=False).select_related('task').order_by('-created_at')[:15]
    tasks_read = TasksNotification.objects.filter(user=request.user, is_read=True).select_related('task').order_by('-created_at')[:15]

    unread_list = []
    for n in tasks_unread:
        unread_list.append({
            "id": n.id,
            "title": n.title,
            "created_at": n.created_at,
            "description": n.description,
            "task": {
                "table_name": n.task.table_name if n.task else "",
                "task_name": n.task.task_name if n.task else "",
            } if n.task else None,
            "type": "tasks",
        })

    read_list = []
    for n in tasks_read:
        read_list.append({
            "id": n.id,
            "title": n.title,
            "created_at": n.created_at,
            "description": n.description,
            "task": {
                "table_name": n.task.table_name if n.task else "",
                "task_name": n.task.task_name if n.task else "",
            } if n.task else None,
            "type": "tasks",
        })

    # Sort lists by created_at descending
    unread_list.sort(key=lambda x: x["created_at"], reverse=True)
    read_list.sort(key=lambda x: x["created_at"], reverse=True)

    sidebar_tables = get_accessible_tables(request.user).select_related('department')

    return {
        "task_notifications_unread": unread_count,
        "unread_notifications": unread_list[:15],
        "read_notifications": read_list[:15],
        "sidebar_trackers": sidebar_tables,
    }

