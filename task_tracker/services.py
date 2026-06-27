from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Q
from django.db.models import Max
from django.utils import timezone

from auth_app.models import EmployeeUser

from .models import FIXED_COLUMN_KEYS
from .models import FIXED_COLUMN_LABELS
from .models import MAIL_STATUS_CHOICES
from .models import TaskCell
from .models import TaskAssignment
from .models import TaskFilter
from .models import TaskRow
from .models import TASK_STATUS_CHOICES
from .models import Tracker
from .models import TrackerColumn
from .models import TaskComment, TaskAttachment, TaskHistory, Notification


@dataclass
class TaskDashboardStats:
    total_tasks: int
    pending_tasks: int
    due_today: int
    overdue_tasks: int
    completed_tasks: int


@transaction.atomic
def ensure_mandatory_columns(tracker: Tracker):
    existing_columns = {
        column.key: column for column in tracker.columns.all().select_for_update()
    }
    created_columns = []
    for position, key in enumerate(FIXED_COLUMN_KEYS, start=1):
        column = existing_columns.get(key)
        if column is None:
            column = TrackerColumn.objects.create(
                tracker=tracker,
                key=key,
                label=FIXED_COLUMN_LABELS[key],
                position=position,
                is_fixed=True,
            )
        else:
            dirty_fields = []
            if column.label != FIXED_COLUMN_LABELS[key]:
                column.label = FIXED_COLUMN_LABELS[key]
                dirty_fields.append("label")
            if column.position != position:
                column.position = position
                dirty_fields.append("position")
            if not column.is_fixed:
                column.is_fixed = True
                dirty_fields.append("is_fixed")
            if dirty_fields:
                column.save(update_fields=dirty_fields + ["updated_at"])
        created_columns.append(column)
    return created_columns


@transaction.atomic
def create_custom_column(tracker: Tracker, label: str):
    custom_columns = tracker.columns.filter(is_fixed=False)
    max_position = tracker.columns.aggregate(max_position=Max("position")).get("max_position") or 0
    base_key = label.strip().replace(" ", "_").upper() or "COLUMN"
    key = base_key
    suffix = 1
    while tracker.columns.filter(key=key).exists():
        suffix += 1
        key = f"{base_key}_{suffix}"

    return TrackerColumn.objects.create(
        tracker=tracker,
        key=key,
        label=label.strip(),
        position=max_position + 1 if custom_columns.exists() else 7,
        is_fixed=False,
    )


@transaction.atomic
def reorder_custom_columns(tracker: Tracker, ordered_column_ids):
    custom_columns = list(tracker.columns.filter(is_fixed=False))
    custom_map = {column.id: column for column in custom_columns}
    next_position = len(FIXED_COLUMN_KEYS) + 1
    for column_id in ordered_column_ids:
        column = custom_map.get(int(column_id))
        if column is None:
            continue
        if column.position != next_position:
            column.position = next_position
            column.save(update_fields=["position", "updated_at"])
        next_position += 1
    for column in custom_columns:
        if column.id not in {int(value) for value in ordered_column_ids}:
            if column.position != next_position:
                column.position = next_position
                column.save(update_fields=["position", "updated_at"])
            next_position += 1


@transaction.atomic
def create_task_row(tracker: Tracker, row_data: dict, dynamic_values: dict[str, str], created_by):
    row = TaskRow.objects.create(
        tracker=tracker,
        s_no=tracker.next_s_no(),
        date=row_data["date"],
        due_date=row_data["due_date"],
        task_name=row_data["task_name"],
        assigned_to=row_data["assigned_to"],
        assigned_by=row_data["assigned_by"],
        status=row_data.get("status", "PENDING"),
        created_by=created_by,
    )

    sync_task_cells(row, dynamic_values)
    TaskAssignment.objects.update_or_create(
        row=row,
        user=row.assigned_to,
        assignment_type="PRIMARY",
        defaults={"assigned_by": row.assigned_by, "is_primary": True},
    )
    TaskHistory.objects.create(
        row=row,
        action="CREATED",
        field_name="task",
        new_value=row.task_name,
        changed_by=created_by,
        metadata={"priority": row.priority},
    )
    handle_initial_email(row)
    return row


@transaction.atomic
def update_task_row(row: TaskRow, row_data: dict, dynamic_values: dict[str, str], updated_by):
    old_assigned_to_id = row.assigned_to_id
    old_due_date = row.due_date
    old_status = row.status
    row.date = row_data["date"]
    row.due_date = row_data["due_date"]
    row.task_name = row_data["task_name"]
    row.priority = row_data.get("priority", row.priority)
    row.assigned_to = row_data["assigned_to"]
    row.assigned_by = row_data["assigned_by"]
    row.status = row_data.get("status", row.status)
    row.save()
    sync_task_cells(row, dynamic_values)
    TaskAssignment.objects.update_or_create(
        row=row,
        user=row.assigned_to,
        assignment_type="PRIMARY",
        defaults={"assigned_by": row.assigned_by, "is_primary": True},
    )
    if old_assigned_to_id != row.assigned_to_id:
        TaskHistory.objects.create(
            row=row,
            action="ASSIGNMENT_CHANGED",
            field_name="assigned_to",
            old_value=str(old_assigned_to_id),
            new_value=str(row.assigned_to_id),
            changed_by=updated_by,
        )
    if old_due_date != row.due_date:
        TaskHistory.objects.create(
            row=row,
            action="DUE_DATE_CHANGED",
            field_name="due_date",
            old_value=str(old_due_date),
            new_value=str(row.due_date),
            changed_by=updated_by,
        )
    if old_status != row.status:
        TaskHistory.objects.create(
            row=row,
            action="STATUS_CHANGED",
            field_name="status",
            old_value=old_status,
            new_value=row.status,
            changed_by=updated_by,
        )
    handle_initial_email(row)
    return row


@transaction.atomic
def sync_task_cells(row: TaskRow, dynamic_values: dict[str, str]):
    dynamic_columns = row.tracker.columns.filter(is_fixed=False)
    cells_by_column_id = {cell.column_id: cell for cell in row.cells.select_related("column")}

    for column in dynamic_columns:
        value = dynamic_values.get(f"column_{column.id}", "")
        cell = cells_by_column_id.get(column.id)
        if cell is None:
            TaskCell.objects.create(row=row, column=column, value=value)
        elif cell.value != value:
            cell.value = value
            cell.save(update_fields=["value", "updated_at"])

    for column_id, cell in cells_by_column_id.items():
        if not dynamic_columns.filter(id=column_id).exists():
            cell.delete()


def handle_initial_email(row: TaskRow):
    assigned_by = getattr(row, 'assigned_by', None)
    assigned_to = getattr(row, 'assigned_to', None)
    
    # Check if assigned by an admin role
    is_assigned_by_admin = assigned_by and getattr(assigned_by, 'role', None) in ['ADMIN', 'SUPER_ADMIN', 'DEPARTMENT_ADMIN']
    if not is_assigned_by_admin:
        return False

    # Do not send email if assignee's role is admin or super admin
    if assigned_to and getattr(assigned_to, 'role', None) in ['ADMIN', 'SUPER_ADMIN']:
        return False

    # Check if initial mail is already sent
    if hasattr(row, 'initial_mail') and row.initial_mail == "YES":
        return True

    task_name = getattr(row, 'task_name', 'Unnamed Task')
    due_date = getattr(row, 'due_date', '')
    subject = f"Task: {task_name} - Due Date: {due_date}"
    message = f"Task Name: {task_name}\nDue Date: {due_date}"

    recipient_email = getattr(assigned_to, 'email', None)
    if recipient_email:
        log_and_send_email(
            subject=subject,
            message=message,
            recipient_list=[recipient_email],
            from_email="operations.flowforce@gmail.com"
        )

    if hasattr(row, 'initial_mail'):
        row.initial_mail = "YES"
        update_fields = ["initial_mail"]
        if hasattr(row, "updated_at"):
            update_fields.append("updated_at")
        row.save(update_fields=update_fields)

    # record history and notification if objects/fields exist
    try:
        TaskHistory.objects.create(
            row=row,
            action="ASSIGNED",
            field_name="assigned_to",
            old_value="",
            new_value=str(getattr(row, 'assigned_to_id', '')),
            changed_by=assigned_by,
        )
    except Exception:
        pass

    try:
        Notification.objects.create(
            user=assigned_to,
            row=row,
            notif_type="ASSIGNMENT",
            payload={"task_name": task_name, "tracker": getattr(getattr(row, 'tracker', None), 'department', '')},
            sent_at=timezone.now(),
        )
    except Exception:
        pass
    return True


@transaction.atomic
def send_due_task_alerts(target_date: date | None = None):
    target_date = target_date or timezone.localdate()
    
    # Safely query, handling potential missing columns
    filter_kwargs = {"due_date": target_date}
    if hasattr(TaskRow, "alert_mail"):
        filter_kwargs["alert_mail"] = "NO"
        
    rows = (
        TaskRow.objects.select_related("assigned_to", "assigned_by", "tracker")
        .filter(**filter_kwargs)
        .exclude(status__in=["COMPLETED", "CANCELLED"])
    )

    sent_count = 0
    for row in rows:
        assigned_to = getattr(row, 'assigned_to', None)
        if assigned_to and getattr(assigned_to, 'role', None) in ['ADMIN', 'SUPER_ADMIN']:
            # Do not send email, but mark as YES to prevent processing again
            if hasattr(row, 'alert_mail'):
                row.alert_mail = "YES"
                update_fields = ["alert_mail"]
                if hasattr(row, 'updated_at'):
                    update_fields.append("updated_at")
                row.save(update_fields=update_fields)
            continue

        task_name = getattr(row, 'task_name', 'Unnamed Task')
        due_date = getattr(row, 'due_date', '')
        subject = f"Task: {task_name} - Due Date: {due_date}"
        message = f"Task Name: {task_name}\nDue Date: {due_date}"

        recipient_email = getattr(assigned_to, 'email', None)
        if recipient_email:
            log_and_send_email(
                subject=subject,
                message=message,
                recipient_list=[recipient_email],
                from_email="operations.flowforce@gmail.com"
            )

        if hasattr(row, 'alert_mail'):
            row.alert_mail = "YES"
            update_fields = ["alert_mail"]
            if hasattr(row, 'updated_at'):
                update_fields.append("updated_at")
            row.save(update_fields=update_fields)
            
        sent_count += 1
        
        try:
            TaskHistory.objects.create(
                row=row,
                action="ALERT_SENT",
                field_name="alert_mail",
                old_value="NO",
                new_value="YES",
                changed_by=None,
            )
        except Exception:
            pass
            
        try:
            Notification.objects.create(
                user=assigned_to,
                row=row,
                notif_type="ALERT",
                payload={"task_name": task_name, "due_date": str(due_date)},
                sent_at=timezone.now(),
            )
        except Exception:
            pass
            
    return sent_count


def send_overdue_escalations():
    today = timezone.localdate()
    overdue_rows = TaskRow.objects.select_related("assigned_to", "tracker").filter(due_date__lt=today).exclude(status__in=["COMPLETED", "CANCELLED"])
    sent = 0
    for row in overdue_rows:
        days_over = (today - row.due_date).days
        if days_over != 6:
            continue
            
        level = "EMPLOYEE"
        targets = [row.assigned_to] if row.assigned_to else []

        if not targets:
            continue

        # avoid duplicate escalation at same level
        existing = Notification.objects.filter(row=row, notif_type="ESCALATION", payload__level=level)
        if existing.exists():
            continue

        subject = f"Overdue Task Escalation - {row.task_name}"
        message = (
            f"Task '{row.task_name}' is overdue by {days_over} days.\n\n"
            f"Department: {row.tracker.department}\n"
            f"Assigned To: {row.assigned_to.full_name if row.assigned_to else 'None'}\n"
            f"Due Date: {row.due_date}\n"
        )

        for user in targets:
            log_and_send_email(subject=subject, message=message, recipient_list=[user.email], from_email="operations.flowforce@gmail.com")
            Notification.objects.create(user=user, row=row, notif_type="ESCALATION", payload={"level": level, "days_overdue": days_over, "task_name": row.task_name}, sent_at=timezone.now())
            sent += 1
        TaskHistory.objects.create(row=row, action="ESCALATION", field_name="overdue", old_value="", new_value=str(days_over), changed_by=None, metadata={"level": level})
    return sent


def get_visible_trackers(user):
    if not user or not user.is_authenticated:
        return Tracker.objects.none()

    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return Tracker.objects.filter(is_active=True).prefetch_related("columns", "tasks")

    from django.db.models import Q
    user_team_ids = list(user.teams.values_list("id", flat=True)) if hasattr(user, "teams") else []

    q_filter = Q(shared_with_users=user)
    if user.department:
        q_filter |= Q(shared_with_departments=user.department)
        if user.role == "DEPARTMENT_ADMIN":
            q_filter |= Q(department=user.department)
    if user_team_ids:
        q_filter |= Q(shared_with_teams__id__in=user_team_ids)

    return Tracker.objects.filter(is_active=True).filter(q_filter).distinct().prefetch_related("columns", "tasks")


def get_tracker_for_department(department: str):
    tracker, _ = Tracker.objects.get_or_create(
        department=department,
        defaults={"name": DEFAULT_TRACKER_NAMES.get(department, f"{department} Task Tracker")},
    )
    ensure_mandatory_columns(tracker)
    return tracker


def get_task_queryset_for_user(user):
    queryset = TaskRow.objects.select_related(
        "tracker",
        "assigned_to",
        "assigned_by",
    ).prefetch_related("cells__column")

    if user.role == "SUPER_ADMIN":
        return queryset
    if user.role == "ADMIN":
        if user.department:
            return queryset.filter(tracker__department=user.department)
        return queryset
    if user.role == "DEPARTMENT_ADMIN":
        return queryset.filter(tracker__department=user.department)
    if user.role == "EMPLOYEE":
        return queryset.filter(assigned_to=user)
    return queryset.none()


def get_dashboard_stats_for_user(user) -> dict:
    from tasks.models import Task
    from django.utils import timezone
    today = timezone.localdate()

    if user.role == "SUPER_ADMIN":
        qs = Task.objects.all()
    elif user.role in ["ADMIN", "DEPARTMENT_ADMIN"]:
        if user.department:
            qs = Task.objects.filter(row__table__department=user.department)
        else:
            qs = Task.objects.all()
    else:
        qs = Task.objects.filter(assigned_to=user)

    return {
        "total_tasks": qs.count(),
        "pending_tasks": qs.filter(status="PENDING").count(),
        "in_progress": qs.filter(status="IN_PROGRESS").count(),
        "ready_for_review": qs.filter(status="READY_FOR_REVIEW").count(),
        "completed_tasks": qs.filter(status="APPROVED").count(),
        "overdue_tasks": qs.filter(due_date__lt=today).exclude(status="APPROVED").count(),
        "due_today": qs.filter(due_date=today).exclude(status="APPROVED").count(),
    }


def attach_row_cells(rows):
    for row in rows:
        row.cells_map = {cell.column_id: cell.value for cell in row.cells.all()}
    return rows


def get_tracker_stats():
    today = timezone.localdate()
    queryset = TaskRow.objects.all()
    return {
        "total_tasks": queryset.count(),
        "pending_tasks": queryset.filter(status="PENDING").count(),
            "due_today": queryset.filter(due_date=today).exclude(status__in=["COMPLETED", "CANCELLED"]).count(),
            "overdue_tasks": queryset.filter(due_date__lt=today).exclude(status__in=["COMPLETED", "CANCELLED"]).count(),
        "completed_tasks": queryset.filter(status="COMPLETED").count(),
    }


def apply_task_filters(queryset, params, tracker=None):
    search = params.get("search")
    department = params.get("department")
    employee = params.get("employee")
    status = params.get("status")
    priority = params.get("priority")
    due_date_from = params.get("due_date_from")
    due_date_to = params.get("due_date_to")
    overdue = params.get("overdue")
    completed = params.get("completed")

    if tracker is not None:
        queryset = queryset.filter(tracker=tracker)
    if search:
        queryset = queryset.filter(
            Q(task_name__icontains=search)
            | Q(assigned_to__full_name__icontains=search)
            | Q(assigned_to__email__icontains=search)
            | Q(tracker__name__icontains=search)
            | Q(tracker__department__name__icontains=search)
            | Q(comments__content__icontains=search)
        ).distinct()
    if department:
        queryset = queryset.filter(tracker__department=department)
    if employee:
        employee_id = getattr(employee, "pk", employee)
        queryset = queryset.filter(Q(assigned_to_id=employee_id) | Q(assignments__user_id=employee_id)).distinct()
    if status:
        queryset = queryset.filter(status=status)
    if priority:
        queryset = queryset.filter(priority=priority)
    if due_date_from:
        queryset = queryset.filter(due_date__gte=due_date_from)
    if due_date_to:
        queryset = queryset.filter(due_date__lte=due_date_to)
    if overdue:
        queryset = queryset.filter(due_date__lt=timezone.localdate()).exclude(status="COMPLETED")
    if completed:
        queryset = queryset.filter(status="COMPLETED")
    return queryset.distinct()


def bulk_update_tasks(task_ids, action, value=None, user=None):
    tasks = TaskRow.objects.filter(id__in=task_ids).select_related("tracker", "assigned_to", "assigned_by")
    affected = 0
    today = timezone.localdate()
    for task in tasks:
        before_status = task.status
        before_due_date = task.due_date
        before_assigned_to = task.assigned_to_id
        if action == "complete":
            task.status = "COMPLETED"
            task.save(update_fields=["status", "updated_at"])
        elif action == "status" and value:
            task.status = value
            task.save(update_fields=["status", "updated_at"])
        elif action == "due_date" and value:
            task.due_date = value
            task.save(update_fields=["due_date", "updated_at"])
        elif action == "assign" and value:
            task.assigned_to_id = value
            task.save(update_fields=["assigned_to", "updated_at"])
            TaskAssignment.objects.update_or_create(
                row=task,
                user_id=value,
                assignment_type="PRIMARY",
                defaults={"assigned_by": user, "is_primary": True},
            )
        elif action == "delete":
            task.delete()
            affected += 1
            continue
        else:
            continue

        if before_status != task.status:
            TaskHistory.objects.create(row=task, action="STATUS_CHANGED", field_name="status", old_value=before_status, new_value=task.status, changed_by=user)
        if before_due_date != task.due_date:
            TaskHistory.objects.create(row=task, action="DUE_DATE_CHANGED", field_name="due_date", old_value=str(before_due_date), new_value=str(task.due_date), changed_by=user)
        if before_assigned_to != task.assigned_to_id:
            TaskHistory.objects.create(row=task, action="ASSIGNMENT_CHANGED", field_name="assigned_to", old_value=str(before_assigned_to), new_value=str(task.assigned_to_id), changed_by=user)
        affected += 1
    return affected


def save_task_filter(user, tracker, name, query_params):
    return TaskFilter.objects.update_or_create(
        user=user,
        tracker=tracker,
        name=name,
        defaults={"query_params": query_params},
    )


def log_and_send_email(subject, message, recipient_list, from_email=None):
    from .tasks import send_async_email_task
    send_async_email_task.delay(subject, message, recipient_list, from_email)
    return True


def log_and_send_email_sync(subject, message, recipient_list, from_email=None):
    from .models import EmailLog
    from django.conf import settings
    
    success = True
    sender = from_email or getattr(settings, 'DEFAULT_FROM_EMAIL', 'operations.flowforce@gmail.com')
    for recipient in recipient_list:
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=sender,
                recipient_list=[recipient],
                fail_silently=False
            )
            EmailLog.objects.create(
                recipient=recipient,
                subject=subject,
                body=message,
                status="SENT"
            )
        except Exception as e:
            success = False
            EmailLog.objects.create(
                recipient=recipient,
                subject=subject,
                body=message,
                status="FAILED",
                error_message=str(e)
            )
    return success

