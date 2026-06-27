from tasks.models import Notification as TasksNotification
from task_tracker.models import Notification as TrackerNotification
from tables.permissions import get_accessible_tables
from django.utils import timezone

def global_context(request):
    if not request.user.is_authenticated:
        return {}

    # Fast count queries for badges
    tasks_unread_count = TasksNotification.objects.filter(user=request.user, is_read=False).count()
    tracker_unread_count = TrackerNotification.objects.filter(user=request.user, read=False).count()
    unread_count = tasks_unread_count + tracker_unread_count

    # Limit lists to 15 items in database query to prevent loading entire history into memory
    tasks_unread = TasksNotification.objects.filter(user=request.user, is_read=False).select_related('task').order_by('-created_at')[:15]
    tasks_read = TasksNotification.objects.filter(user=request.user, is_read=True).select_related('task').order_by('-created_at')[:15]

    tracker_unread = TrackerNotification.objects.filter(user=request.user, read=False).select_related('row', 'row__tracker').order_by('-sent_at')[:15]
    tracker_read = TrackerNotification.objects.filter(user=request.user, read=True).select_related('row', 'row__tracker').order_by('-sent_at')[:15]

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
    for n in tracker_unread:
        desc = ""
        if n.notif_type == "ASSIGNMENT":
            desc = f"You have been assigned task '{n.payload.get('task_name', '')}' in department '{n.payload.get('tracker', '')}'."
        elif n.notif_type == "ALERT":
            desc = f"Task '{n.payload.get('task_name', '')}' is due today."
        elif n.notif_type == "ESCALATION":
            desc = f"Task '{n.payload.get('task_name', '')}' is overdue by {n.payload.get('days_overdue', 0)} days."
        unread_list.append({
            "id": n.id,
            "title": f"{n.get_notif_type_display()} Notification",
            "created_at": n.sent_at or timezone.now(),
            "description": desc,
            "task": {
                "table_name": n.row.tracker.name if n.row else "",
                "task_name": n.row.task_name if n.row else "",
            } if n.row else None,
            "type": "tracker",
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
    for n in tracker_read:
        desc = ""
        if n.notif_type == "ASSIGNMENT":
            desc = f"You have been assigned task '{n.payload.get('task_name', '')}' in department '{n.payload.get('tracker', '')}'."
        elif n.notif_type == "ALERT":
            desc = f"Task '{n.payload.get('task_name', '')}' is due today."
        elif n.notif_type == "ESCALATION":
            desc = f"Task '{n.payload.get('task_name', '')}' is overdue by {n.payload.get('days_overdue', 0)} days."
        read_list.append({
            "id": n.id,
            "title": f"{n.get_notif_type_display()} Notification",
            "created_at": n.sent_at or timezone.now(),
            "description": desc,
            "task": {
                "table_name": n.row.tracker.name if n.row else "",
                "task_name": n.row.task_name if n.row else "",
            } if n.row else None,
            "type": "tracker",
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

