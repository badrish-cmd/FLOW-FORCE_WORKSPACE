from django.db.models import Prefetch
from tasks.models import Notification as TasksNotification
from tables.models import CellValue
from tables.permissions import get_accessible_tables

TASK_NAME_COLUMNS = [
    "CUSTOMER_NAME", "TASK_NAME", "TASK NAME", "NAME", "TITLE", "SUBJECT", "TASK",
    "ENQUIRY_NO", "ENQUIRY_NO/QUOTATION_NO", "ENQUIRY_NO_QUOTATION_NO", "ENQUIRY NUMBER", "ENQUIRY NO", "ENQUIRY_NO / QUOTATION_NO", "PID",
    "TOOL_NAME", "TOOL NAME", "TOOL"
]

def global_context(request):
    if not request.user.is_authenticated:
        return {}

    # Fast count queries for badges
    unread_count = TasksNotification.objects.filter(user=request.user, is_read=False).count()

    # Targeted prefetch for column cell values needed for task names
    cells_prefetch = Prefetch(
        'task__row__cells',
        queryset=CellValue.objects.filter(column__name__in=TASK_NAME_COLUMNS).select_related('column')
    )

    # Limit lists to 15 items in database query to prevent loading entire history into memory
    tasks_unread = TasksNotification.objects.filter(
        user=request.user, is_read=False
    ).select_related(
        'task__row__table'
    ).prefetch_related(
        cells_prefetch
    ).order_by('-created_at')[:15]

    tasks_read = TasksNotification.objects.filter(
        user=request.user, is_read=True
    ).select_related(
        'task__row__table'
    ).prefetch_related(
        cells_prefetch
    ).order_by('-created_at')[:15]

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

    from django.utils.functional import SimpleLazyObject
    from tasks.models import Announcement

    def _get_active_announcement():
        return Announcement.objects.filter(
            is_published=True
        ).exclude(
            reads__employee=request.user
        ).order_by('-published_at', '-id').first()

    return {
        "task_notifications_unread": unread_count,
        "unread_notifications": unread_list[:15],
        "read_notifications": read_list[:15],
        "sidebar_trackers": sidebar_tables,
        "active_announcement": SimpleLazyObject(_get_active_announcement),
    }
