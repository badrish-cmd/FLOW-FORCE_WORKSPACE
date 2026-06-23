import csv
import io
from urllib.parse import parse_qs
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.http import require_POST
from django.utils.dateparse import parse_date
from django.utils import timezone

from .forms import TaskFilterForm, TaskFilterSaveForm, TaskImportForm
from .forms import TaskRowForm
from .forms import TrackerColumnForm
from .forms import TrackerForm
from .forms import CommentForm, AttachmentForm
from .models import TaskFilter
from .models import TaskRow
from .models import Tracker
from .permissions import can_manage_tasks
from .permissions import can_manage_tracker_columns
from .permissions import can_manage_trackers
from .permissions import can_update_task_status
from .permissions import can_view_task
from .permissions import tracker_access_required
from .permissions import tracker_manage_required
from .services import attach_row_cells
from .services import apply_task_filters
from .services import bulk_update_tasks
from .services import create_task_row
from .services import ensure_mandatory_columns
from .services import get_dashboard_stats_for_user
from .services import get_visible_trackers
from .services import reorder_custom_columns
from .services import save_task_filter
from .services import create_custom_column
from .services import update_task_row
from .services import handle_initial_email
from .models import Notification, TaskComment, TaskAttachment, TaskHistory


@login_required
@tracker_access_required
def tracker_dashboard(request):
    from tables.models import Table
    from tables.permissions import get_accessible_tables
    from tasks.models import Task

    if request.user.role in ["SUPER_ADMIN", "ADMIN"]:
        tables = Table.objects.filter(is_active=True)
    else:
        tables = get_accessible_tables(request.user)

    tables_data = []
    for table in tables:
        tasks = Task.objects.filter(row__table=table, row__is_archived=False)
        total = tasks.count()
        pending = tasks.filter(status="PENDING").count()
        in_progress = tasks.filter(status="IN_PROGRESS").count()
        ready_for_review = tasks.filter(status="READY_FOR_REVIEW").count()
        completed = tasks.filter(status__in=["COMPLETED", "APPROVED"]).count()

        overdue = tasks.filter(due_date__lt=timezone.localdate()).exclude(status__in=["COMPLETED", "APPROVED"]).count()
        due_today = tasks.filter(due_date=timezone.localdate()).count()

        low = tasks.filter(priority="LOW").count()
        medium = tasks.filter(priority="MEDIUM").count()
        high = tasks.filter(priority="HIGH").count()
        critical = tasks.filter(priority="CRITICAL").count()

        completion_rate = int(completed * 100 / total) if total > 0 else 0

        tables_data.append({
            "table": table,
            "total": total,
            "pending": pending,
            "in_progress": in_progress,
            "ready_for_review": ready_for_review,
            "completed": completed,
            "overdue": overdue,
            "due_today": due_today,
            "low": low,
            "medium": medium,
            "high": high,
            "critical": critical,
            "completion_rate": completion_rate,
        })

    return render(
        request,
        "task_tracker/dashboard.html",
        {
            "tables_data": tables_data,
        },
    )



@login_required
@tracker_access_required
def tracker_detail(request, tracker_id):
    return redirect("task_tracker:tracker_spreadsheet", tracker_id=tracker_id)


@login_required
@tracker_manage_required
def tracker_create(request):
    if request.method == "POST":
        form = TrackerForm(request.POST)
        if form.is_valid():
            tracker = form.save(commit=True)
            ensure_mandatory_columns(tracker)
            messages.success(request, "Tracker created successfully.")
            return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    else:
        form = TrackerForm()
    return render(request, "task_tracker/tracker_form.html", {"form": form, "page_title": "Create Tracker"})


@login_required
@tracker_manage_required
def tracker_edit(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if request.method == "POST":
        form = TrackerForm(request.POST, instance=tracker)
        if form.is_valid():
            tracker = form.save()
            messages.success(request, "Tracker updated successfully.")
            return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    else:
        form = TrackerForm(instance=tracker)
    return render(request, "task_tracker/tracker_form.html", {"form": form, "tracker": tracker, "page_title": "Edit Tracker"})


@login_required
@tracker_manage_required
@require_POST
def tracker_delete(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    tracker_name = tracker.name
    tracker.delete()
    messages.success(request, f"Tracker '{tracker_name}' deleted successfully.")
    return redirect("task_tracker:tracker_dashboard")


@login_required
@tracker_access_required
@require_POST
def tracker_column_create(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_tracker_columns(request.user, tracker):
        messages.error(request, "You do not have permission to manage columns.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    form = TrackerColumnForm(request.POST, tracker=tracker)
    if form.is_valid():
        form.save()
        messages.success(request, "Custom column created.")
    else:
        messages.error(request, "Unable to create custom column.")
    return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)


@login_required
@tracker_access_required
@require_POST
def tracker_column_update(request, tracker_id, column_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    column = get_object_or_404(tracker.columns.all(), pk=column_id)
    if column.is_fixed or not can_manage_tracker_columns(request.user, tracker):
        messages.error(request, "Fixed columns cannot be edited.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    form = TrackerColumnForm(request.POST, instance=column, tracker=tracker)
    if form.is_valid():
        form.save()
        messages.success(request, "Column updated.")
    else:
        messages.error(request, "Unable to update column.")
    return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)


@login_required
@tracker_access_required
@require_POST
def tracker_column_delete(request, tracker_id, column_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    column = get_object_or_404(tracker.columns.all(), pk=column_id)
    if column.is_fixed or not can_manage_tracker_columns(request.user, tracker):
        messages.error(request, "Fixed columns cannot be deleted.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    column.delete()
    messages.success(request, "Column deleted.")
    return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)


@login_required
@tracker_access_required
@require_POST
def tracker_column_reorder(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_tracker_columns(request.user, tracker):
        messages.error(request, "You do not have permission to reorder columns.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    ordered_ids = request.POST.getlist("column_ids")
    reorder_custom_columns(tracker, ordered_ids)
    messages.success(request, "Column order updated.")
    return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)


@login_required
@tracker_access_required
def task_create(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_tasks(request.user, tracker):
        messages.error(request, "You do not have permission to assign tasks.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    if request.method == "POST":
        form = TaskRowForm(request.POST, tracker=tracker, request_user=request.user)
        if form.is_valid():
            row = create_task_row(
                tracker,
                {
                    "date": form.cleaned_data["date"],
                    "due_date": form.cleaned_data["due_date"],
                    "task_name": form.cleaned_data["task_name"],
                    "priority": form.cleaned_data.get("priority") or "MEDIUM",
                    "assigned_to": form.cleaned_data["assigned_to"],
                    "assigned_by": form.cleaned_data["assigned_by"],
                    "status": form.cleaned_data["status"],
                },
                form.dynamic_values(),
                request.user,
            )
            messages.success(request, "Task created successfully.")
            return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=row.id)
    else:
        form = TaskRowForm(
            tracker=tracker,
            request_user=request.user,
            initial={"date": request.POST.get("date")},
        )
    return render(request, "task_tracker/task_form.html", {"tracker": tracker, "form": form, "page_title": "Create Task"})


@login_required
@tracker_access_required
def task_edit(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow.objects.select_related("tracker"), pk=task_id, tracker=tracker)
    if not can_manage_tasks(request.user, tracker):
        messages.error(request, "You do not have permission to edit tasks in this tracker.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    if request.method == "POST":
        form = TaskRowForm(request.POST, tracker=tracker, request_user=request.user, instance=task)
        if form.is_valid():
            update_task_row(
                task,
                {
                    "date": form.cleaned_data["date"],
                    "due_date": form.cleaned_data["due_date"],
                    "task_name": form.cleaned_data["task_name"],
                    "priority": form.cleaned_data.get("priority") or task.priority,
                    "assigned_to": form.cleaned_data["assigned_to"],
                    "assigned_by": form.cleaned_data["assigned_by"],
                    "status": form.cleaned_data["status"],
                },
                form.dynamic_values(),
                request.user,
            )
            messages.success(request, "Task updated successfully.")
            return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)
    else:
        form = TaskRowForm(tracker=tracker, request_user=request.user, instance=task)
    return render(request, "task_tracker/task_form.html", {"tracker": tracker, "task": task, "form": form, "page_title": "Edit Task"})


@login_required
@tracker_access_required
def task_detail(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow.objects.select_related("tracker", "assigned_to", "assigned_by").prefetch_related("cells__column"), pk=task_id, tracker=tracker)
    if not can_view_task(request.user, task):
        messages.error(request, "You do not have permission to view this task.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    task.cells_map = {cell.column_id: cell.value for cell in task.cells.all()}
    return render(
        request,
        "task_tracker/task_detail.html",
        {
            "tracker": tracker,
            "task": task,
            "columns": tracker.columns.all().order_by("position", "id"),
            "can_edit": can_manage_tasks(request.user, tracker),
            "can_update_status": can_update_task_status(request.user, task),
            "comment_form": CommentForm(),
            "attachment_form": AttachmentForm(),
        },
    )


@login_required
def notification_mark_read(request, notification_id):
    notification = get_object_or_404(Notification, pk=notification_id, user=request.user)
    notification.read = True
    notification.save(update_fields=["read"])
    if notification.row:
        return redirect("task_tracker:task_detail", tracker_id=notification.row.tracker.id, task_id=notification.row.id)
    return redirect("task_tracker:tracker_dashboard")


@login_required
@require_POST
def task_status_update(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    if not can_update_task_status(request.user, task):
        messages.error(request, "You do not have permission to update this task.")
        return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)
    new_status = request.POST.get("status")
    valid_statuses = {choice for choice, _ in task._meta.get_field("status").choices}
    if new_status not in valid_statuses:
        messages.error(request, "Invalid status.")
        return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)
    task.status = new_status
    task.save(update_fields=["status", "updated_at"])
    messages.success(request, "Task status updated.")
    return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)


def _base_tracker_queryset(request, tracker):
    if request.user.role == "EMPLOYEE":
        return tracker.tasks.filter(assigned_to=request.user)
    if request.user.role == "DEPARTMENT_ADMIN":
        return tracker.tasks.filter(tracker__department=request.user.department)
    return tracker.tasks.all()


def _render_tracker_board(request, tracker, view_mode="spreadsheet"):
    columns = list(tracker.columns.all().order_by("position", "id"))
    base_queryset = _base_tracker_queryset(request, tracker).select_related("assigned_to", "assigned_by").prefetch_related("cells__column", "comments", "attachments", "history", "assignments")

    filter_form = TaskFilterForm(request.GET or None, tracker=tracker, request_user=request.user)
    queryset = base_queryset
    if filter_form.is_valid():
        queryset = apply_task_filters(queryset, filter_form.cleaned_data, tracker=tracker)

    tasks = attach_row_cells(queryset)

    grouped_tasks = {}
    if view_mode == "kanban":
        kanban_statuses = ["PENDING", "IN_PROGRESS", "ON_HOLD", "COMPLETED", "CANCELLED"]
        for status in kanban_statuses:
            grouped_tasks[status] = [task for task in tasks if task.status == status]
    elif view_mode == "calendar":
        for task in tasks:
            grouped_tasks.setdefault(str(task.due_date), []).append(task)
    elif view_mode == "timeline":
        for task in tasks:
            grouped_tasks.setdefault(task.due_date.strftime("%Y-%m"), []).append(task)

    saved_filters = TaskFilter.objects.filter(user=request.user, tracker=tracker).order_by("name")
    saved_filters_serialized = [
        {"id": saved_filter.id, "name": saved_filter.name, "query_string": urlencode(saved_filter.query_params, doseq=True)}
        for saved_filter in saved_filters
    ]

    from employee_management.models import Department, Team
    all_users = EmployeeUser.objects.filter(is_active=True).order_by("full_name")
    all_departments = Department.objects.filter(is_active=True).order_by("name")
    all_teams = Team.objects.all().order_by("name")

    return render(
        request,
        "task_tracker/tracker_board.html",
        {
            "tracker": tracker,
            "columns": columns,
            "custom_columns": [column for column in columns if not column.is_fixed],
            "tasks": tasks,
            "grouped_tasks": grouped_tasks,
            "view_mode": view_mode,
            "filter_form": filter_form,
            "saved_filters": saved_filters_serialized,
            "import_form": TaskImportForm(),
            "can_manage_columns": can_manage_tracker_columns(request.user, tracker),
            "can_manage_tasks": can_manage_tasks(request.user, tracker),
            "can_update_status": request.user.role in ("SUPER_ADMIN", "ADMIN", "DEPARTMENT_ADMIN", "EMPLOYEE"),
            "all_users": all_users,
            "all_departments": all_departments,
            "all_teams": all_teams,
        },
    )


@login_required
@tracker_access_required
def tracker_spreadsheet(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    ensure_mandatory_columns(tracker)
    return _render_tracker_board(request, tracker, view_mode="spreadsheet")


@login_required
@tracker_access_required
def tracker_kanban(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    ensure_mandatory_columns(tracker)
    return _render_tracker_board(request, tracker, view_mode="kanban")


@login_required
@tracker_access_required
def tracker_calendar(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    ensure_mandatory_columns(tracker)
    return _render_tracker_board(request, tracker, view_mode="calendar")


@login_required
@tracker_access_required
def tracker_timeline(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    ensure_mandatory_columns(tracker)
    return _render_tracker_board(request, tracker, view_mode="timeline")


@login_required
@tracker_access_required
def my_tasks(request):
    tasks = TaskRow.objects.select_related("tracker", "assigned_to", "assigned_by").filter(assigned_to=request.user)
    tasks = apply_task_filters(tasks, request.GET, tracker=None)
    tasks = attach_row_cells(tasks)
    return render(request, "task_tracker/tracker_board.html", {"tracker": None, "view_mode": "spreadsheet", "tasks": tasks, "columns": [], "grouped_tasks": {}, "filter_form": TaskFilterForm(request.GET or None, request_user=request.user), "saved_filters": []})


@login_required
@tracker_access_required
def overdue_tasks(request):
    tasks = TaskRow.objects.select_related("tracker", "assigned_to", "assigned_by").filter(due_date__lt=timezone.localdate()).exclude(status="COMPLETED")
    tasks = apply_task_filters(tasks, request.GET, tracker=None)
    tasks = attach_row_cells(tasks)
    return render(request, "task_tracker/tracker_board.html", {"tracker": None, "view_mode": "spreadsheet", "tasks": tasks, "columns": [], "grouped_tasks": {}, "filter_form": TaskFilterForm(request.GET or None, request_user=request.user), "saved_filters": []})


@login_required
@tracker_access_required
@require_POST
def bulk_tasks(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_tasks(request.user, tracker):
        messages.error(request, "You do not have permission to perform bulk actions.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)
    task_ids = request.POST.getlist("task_ids")
    action = request.POST.get("bulk_action")
    value = request.POST.get("bulk_value")
    if action == "due_date" and value:
        value = parse_date(value)
    affected = bulk_update_tasks(task_ids, action, value=value, user=request.user)
    messages.success(request, f"Bulk action applied to {affected} task(s).")
    return redirect(request.POST.get("next") or "task_tracker:tracker_detail", tracker_id=tracker.id)


@login_required
@tracker_access_required
@require_POST
def save_filter(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    form = TaskFilterSaveForm(request.POST)
    if form.is_valid():
        query_string = request.POST.get("saved_query", "")
        query_params = {key: values[-1] if len(values) == 1 else values for key, values in parse_qs(query_string).items()}
        save_task_filter(request.user, tracker, form.cleaned_data["name"], query_params)
        messages.success(request, "Filter saved.")
    else:
        messages.error(request, "Unable to save filter.")
    return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)


@login_required
@tracker_access_required
def apply_saved_filter(request, tracker_id, filter_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    saved_filter = get_object_or_404(TaskFilter, pk=filter_id, user=request.user, tracker=tracker)
    query_string = urlencode(saved_filter.query_params, doseq=True)
    return redirect(f"/trackers/{tracker.id}/?{query_string}" if query_string else f"/trackers/{tracker.id}/")


@login_required
@tracker_access_required
def export_tasks_csv(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    queryset = _base_tracker_queryset(request, tracker).select_related("assigned_to", "assigned_by", "tracker")
    queryset = apply_task_filters(queryset, request.GET, tracker=tracker)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["S_NO", "DATE", "DUE_DATE", "TASK_NAME", "PRIORITY", "STATUS", "ASSIGNED_TO", "ASSIGNED_BY"])
    for task in queryset:
        writer.writerow([task.s_no, task.date, task.due_date, task.task_name, task.priority, task.status, task.assigned_to.email, task.assigned_by.email])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{tracker.department.lower()}_tasks.csv"'
    return response


@login_required
@tracker_access_required
@require_POST
def import_tasks_csv(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_tasks(request.user, tracker):
        messages.error(request, "You do not have permission to import tasks.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)

    form = TaskImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Please upload a valid CSV file.")
        return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)

    uploaded_file = form.cleaned_data["file"]
    decoded = uploaded_file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(decoded))
    existing_columns = {column.key: column for column in tracker.columns.all()}
    existing_columns.update({column.label.upper().replace(" ", "_"): column for column in tracker.columns.all()})
    imported = 0

    for row_data in reader:
        assigned_to_email = row_data.get("ASSIGNED_TO") or row_data.get("ASSIGNED_TO_EMAIL")
        assigned_by_email = row_data.get("ASSIGNED_BY") or row_data.get("ASSIGNED_BY_EMAIL") or request.user.email
        try:
            assigned_to = request.user.__class__.objects.get(email=assigned_to_email)
            assigned_by = request.user.__class__.objects.get(email=assigned_by_email)
        except Exception:
            continue

        dynamic_values = {}
        for key, value in row_data.items():
            normalized_key = key.strip().upper().replace(" ", "_")
            column = existing_columns.get(normalized_key)
            if column and not column.is_fixed:
                dynamic_values[f"column_{column.id}"] = value

        create_task_row(
            tracker,
            {
                "date": parse_date(row_data.get("DATE") or str(timezone.localdate())) or timezone.localdate(),
                "due_date": parse_date(row_data.get("DUE_DATE") or "") or timezone.localdate(),
                "task_name": row_data.get("TASK_NAME") or "Imported Task",
                "priority": row_data.get("PRIORITY") or "MEDIUM",
                "assigned_to": assigned_to,
                "assigned_by": assigned_by,
                "status": row_data.get("STATUS") or "PENDING",
            },
            dynamic_values,
            request.user,
        )
        imported += 1

    messages.success(request, f"Imported {imported} task(s) from CSV.")
    return redirect("task_tracker:tracker_detail", tracker_id=tracker.id)


@login_required
@require_POST
def comment_create(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    if not can_view_task(request.user, task):
        messages.error(request, "You do not have permission to comment on this task.")
        return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)
    form = CommentForm(request.POST)
    if form.is_valid():
        parent = None
        parent_id = form.cleaned_data.get("parent_id")
        if parent_id:
            try:
                parent = TaskComment.objects.get(pk=parent_id, row=task)
            except TaskComment.DoesNotExist:
                parent = None
        comment = TaskComment.objects.create(
            row=task,
            parent=parent,
            content=form.cleaned_data["content"],
            internal=form.cleaned_data.get("internal", False),
            created_by=request.user,
        )
        TaskHistory.objects.create(row=task, action="COMMENT", field_name="comment", new_value=comment.content, changed_by=request.user)
        messages.success(request, "Comment posted.")
    else:
        messages.error(request, "Unable to post comment.")
    return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)


@login_required
@require_POST
def attachment_upload(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    if not can_manage_tasks(request.user, tracker) and request.user != task.assigned_to:
        messages.error(request, "You do not have permission to upload attachments for this task.")
        return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        f = form.cleaned_data["file"]
        attach = TaskAttachment.objects.create(
            row=task,
            file=f,
            original_name=getattr(f, "name", ""),
            content_type=getattr(f, "content_type", ""),
            size=getattr(f, "size", None),
            uploaded_by=request.user,
        )
        TaskHistory.objects.create(row=task, action="ATTACHMENT_ADDED", field_name="attachment", new_value=attach.original_name, changed_by=request.user)
        messages.success(request, "Attachment uploaded.")
    else:
        messages.error(request, "Unable to upload attachment.")
    return redirect("task_tracker:task_detail", tracker_id=tracker.id, task_id=task.id)


# ============================================================================
# DYNAMIC SPREADSHEET ENGINE AJAX ENDPOINTS
# ============================================================================

import json
from datetime import timedelta
from django.http import JsonResponse
from .models import FIXED_COLUMN_KEYS, TrackerColumn, TrackerCell, TaskAssignment, TaskComment, TaskHistory, Notification
from auth_app.models import EmployeeUser

@login_required
@require_POST
def ajax_cell_edit(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    
    if not can_update_task_status(request.user, task):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    try:
        data = json.loads(request.body)
        column_key = data.get("column_key")
        value = data.get("value", "").strip()
    except Exception:
        return JsonResponse({"status": "error", "message": "Invalid JSON payload"}, status=400)
        
    if not column_key:
        return JsonResponse({"status": "error", "message": "Missing column key"}, status=400)
        
    # Check if column is fixed or is a standard TaskRow model attribute
    if column_key in FIXED_COLUMN_KEYS or column_key in ["ASSIGNED_TO", "PRIORITY", "STATUS"]:
        if column_key in ["S_NO", "INITIAL_MAIL", "ALERT_MAIL"]:
            return JsonResponse({"status": "error", "message": "This field is system-controlled and cannot be edited"}, status=400)
            
        if not can_manage_tasks(request.user, tracker) and request.user != task.assigned_to:
            return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
            
        old_val = ""
        update_fields = [column_key.lower(), "updated_at"]
        
        if column_key == "DATE":
            old_val = str(task.date)
            parsed_date = parse_date(value)
            if not parsed_date:
                return JsonResponse({"status": "error", "message": "Invalid Date format"}, status=400)
            task.date = parsed_date
        elif column_key == "DUE_DATE":
            old_val = str(task.due_date)
            parsed_date = parse_date(value)
            if not parsed_date:
                return JsonResponse({"status": "error", "message": "Invalid Date format"}, status=400)
            task.due_date = parsed_date
        elif column_key == "TASK_NAME":
            old_val = task.task_name
            if not value:
                return JsonResponse({"status": "error", "message": "Task Name is required"}, status=400)
            task.task_name = value
        elif column_key == "ASSIGNED_TO":
            old_val = task.assigned_to.email if task.assigned_to else ""
            from django.db.models import Q
            try:
                if "@" in value:
                    new_user = EmployeeUser.objects.get(email=value, is_active=True)
                elif str(value).isdigit():
                    new_user = EmployeeUser.objects.get(id=int(value), is_active=True)
                else:
                    new_user = EmployeeUser.objects.get(full_name__iexact=value, is_active=True)
            except (EmployeeUser.DoesNotExist, ValueError):
                new_user = EmployeeUser.objects.filter(
                    Q(full_name__icontains=value) | Q(email__icontains=value),
                    is_active=True
                ).first()
                if not new_user:
                    return JsonResponse({"status": "error", "message": "Invalid User selected"}, status=400)
            task.assigned_to = new_user
            update_fields = ["assigned_to", "updated_at"]
            # Create a TaskAssignment
            TaskAssignment.objects.create(
                row=task,
                user=new_user,
                assignment_type="PRIMARY",
                assigned_by=request.user,
                is_primary=True
            )
            # Send assignment email
            handle_initial_email(task)
            value = new_user.full_name
        elif column_key == "PRIORITY":
            from .models import TASK_PRIORITY_CHOICES
            old_val = task.priority
            valid_priorities = [choice[0] for choice in TASK_PRIORITY_CHOICES]
            if value.upper() not in valid_priorities:
                return JsonResponse({"status": "error", "message": f"Invalid Priority. Must be one of: {', '.join(valid_priorities)}"}, status=400)
            task.priority = value.upper()
            value = task.priority
        elif column_key == "STATUS":
            from .models import TASK_STATUS_CHOICES
            old_val = task.status
            valid_statuses = [choice[0] for choice in TASK_STATUS_CHOICES]
            if value.upper() not in valid_statuses:
                return JsonResponse({"status": "error", "message": f"Invalid Status. Must be one of: {', '.join(valid_statuses)}"}, status=400)
            task.status = value.upper()
            value = task.status
            
        task.save(update_fields=update_fields)
        TaskHistory.objects.create(
            row=task,
            action="CELL_UPDATE",
            field_name=column_key,
            old_value=old_val,
            new_value=value,
            changed_by=request.user
        )
        return JsonResponse({"status": "success", "row_id": task.id, "column_key": column_key, "value": value})
        
    # It is a custom column
    try:
        if column_key.startswith("COLUMN_"):
            col_id = int(column_key.split("_")[1])
            column = tracker.columns.get(id=col_id)
        else:
            column = tracker.columns.get(key=column_key)
    except (TrackerColumn.DoesNotExist, ValueError):
        return JsonResponse({"status": "error", "message": "Column not found"}, status=404)
        
    if column.permission_level == "ADMIN_ONLY" and request.user.role not in ("SUPER_ADMIN", "ADMIN"):
        return JsonResponse({"status": "error", "message": "Only admins can edit this column"}, status=403)
    elif column.permission_level == "DEPT_ADMIN_AND_ABOVE" and request.user.role not in ("SUPER_ADMIN", "ADMIN", "DEPARTMENT_ADMIN"):
        return JsonResponse({"status": "error", "message": "Only department admins and above can edit this column"}, status=403)
        
    if column.is_read_only or column.is_locked:
        return JsonResponse({"status": "error", "message": "This column is locked or read-only"}, status=400)
        
    if column.required and not value:
        return JsonResponse({"status": "error", "message": f"{column.label} is required"}, status=400)
        
    if column.unique and value:
        already_exists = TrackerCell.objects.filter(column=column, value=value).exclude(row=task).exists()
        if already_exists:
            return JsonResponse({"status": "error", "message": f"Value '{value}' must be unique in column '{column.label}'"}, status=400)
            
    if column.column_type == "DROPDOWN" and value:
        choices_list = [c.strip() for c in column.choices.split(",") if c.strip()]
        if value not in choices_list:
            return JsonResponse({"status": "error", "message": f"Value must be one of: {', '.join(choices_list)}"}, status=400)
            
    cell, created = TrackerCell.objects.get_or_create(row=task, column=column, defaults={"value": ""})
    old_val = cell.value
    cell.value = value
    cell.save()
    
    TaskHistory.objects.create(
        row=task,
        action="CELL_UPDATE",
        field_name=column.label,
        old_value=old_val,
        new_value=value,
        changed_by=request.user
    )
    return JsonResponse({"status": "success", "row_id": task.id, "column_key": column_key, "value": value})


@login_required
@require_POST
def ajax_add_row(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_tasks(request.user, tracker):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    today = timezone.localdate()
    default_assignee = EmployeeUser.objects.filter(department=tracker.department, is_active=True).first() or request.user
    
    row = TaskRow.objects.create(
        tracker=tracker,
        s_no=tracker.next_s_no(),
        date=today,
        due_date=today + timedelta(days=7),
        task_name="New Task",
        assigned_to=default_assignee,
        assigned_by=request.user,
        status="PENDING",
        created_by=request.user
    )
    
    for column in tracker.columns.filter(is_fixed=False):
        TrackerCell.objects.create(row=row, column=column, value=column.default_value)
        
    TaskAssignment.objects.create(
        row=row,
        user=row.assigned_to,
        assignment_type="PRIMARY",
        assigned_by=request.user,
        is_primary=True
    )
    
    handle_initial_email(row)
    
    cells_data = {str(cell.column.id): cell.value for cell in row.cells.all()}
    return JsonResponse({
        "status": "success",
        "row": {
            "id": row.id,
            "s_no": row.s_no,
            "date": str(row.date),
            "due_date": str(row.due_date),
            "task_name": row.task_name,
            "assigned_to": row.assigned_to.full_name if row.assigned_to else "",
            "assigned_to_id": row.assigned_to.id if row.assigned_to else "",
            "assigned_to_email": row.assigned_to.email if row.assigned_to else "",
            "priority": row.priority,
            "status": row.status,
            "cells": cells_data
        }
    })


@login_required
@require_POST
def ajax_duplicate_row(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    if not can_manage_tasks(request.user, tracker):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    new_row = TaskRow.objects.create(
        tracker=tracker,
        s_no=tracker.next_s_no(),
        date=timezone.localdate(),
        due_date=task.due_date,
        task_name=f"{task.task_name} (Copy)",
        assigned_to=task.assigned_to,
        assigned_by=request.user,
        status="PENDING",
        priority=task.priority,
        created_by=request.user
    )
    
    for cell in task.cells.all():
        TrackerCell.objects.create(row=new_row, column=cell.column, value=cell.value)
        
    TaskAssignment.objects.create(
        row=new_row,
        user=new_row.assigned_to,
        assignment_type="PRIMARY",
        assigned_by=request.user,
        is_primary=True
    )
    
    cells_data = {str(cell.column.id): cell.value for cell in new_row.cells.all()}
    return JsonResponse({
        "status": "success",
        "row": {
            "id": new_row.id,
            "s_no": new_row.s_no,
            "date": str(new_row.date),
            "due_date": str(new_row.due_date),
            "task_name": new_row.task_name,
            "assigned_to": new_row.assigned_to.full_name if new_row.assigned_to else "",
            "assigned_to_id": new_row.assigned_to.id if new_row.assigned_to else "",
            "assigned_to_email": new_row.assigned_to.email if new_row.assigned_to else "",
            "priority": new_row.priority,
            "status": new_row.status,
            "cells": cells_data
        }
    })


@login_required
@require_POST
def ajax_delete_row(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    if not can_manage_tasks(request.user, tracker):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    task.is_archived = True
    task.save(update_fields=["is_archived", "updated_at"])
    
    TaskHistory.objects.create(
        row=task,
        action="ARCHIVED",
        field_name="is_archived",
        old_value="False",
        new_value="True",
        changed_by=request.user
    )
    return JsonResponse({"status": "success", "row_id": task.id})


@login_required
@require_POST
def ajax_restore_row(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    if not can_manage_tasks(request.user, tracker):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    task.is_archived = False
    task.save(update_fields=["is_archived", "updated_at"])
    
    TaskHistory.objects.create(
        row=task,
        action="RESTORED",
        field_name="is_archived",
        old_value="True",
        new_value="False",
        changed_by=request.user
    )
    return JsonResponse({"status": "success", "row_id": task.id})


@login_required
@require_POST
def ajax_add_column(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_tracker_columns(request.user, tracker):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    try:
        data = json.loads(request.body)
        label = data.get("label", "").strip()
        col_type = data.get("column_type", "TEXT").upper()
        choices = data.get("choices", "").strip()
        is_frozen = data.get("is_frozen", False)
        is_hidden = data.get("is_hidden", False)
        is_read_only = data.get("is_read_only", False)
        is_locked = data.get("is_locked", False)
        required = data.get("required", False)
        unique = data.get("unique", False)
        default_value = data.get("default_value", "").strip()
        permission_level = data.get("permission_level", "ALL_EDITABLE")
    except Exception:
        return JsonResponse({"status": "error", "message": "Invalid payload"}, status=400)
        
    if not label:
        return JsonResponse({"status": "error", "message": "Column name is required"}, status=400)
        
    column = create_custom_column(tracker, label)
    column.column_type = col_type
    column.choices = choices
    column.is_frozen = is_frozen
    column.is_hidden = is_hidden
    column.is_read_only = is_read_only
    column.is_locked = is_locked
    column.required = required
    column.unique = unique
    column.default_value = default_value
    column.permission_level = permission_level
    column.save()
    
    # Initialize cells for all tasks in this tracker
    for task in tracker.tasks.all():
        TrackerCell.objects.get_or_create(row=task, column=column, defaults={"value": default_value})
        
    return JsonResponse({
        "status": "success",
        "column": {
            "id": column.id,
            "key": column.key,
            "label": column.label,
            "position": column.position,
            "column_type": column.column_type
        }
    })


@login_required
@require_POST
def ajax_edit_column(request, tracker_id, column_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    column = get_object_or_404(tracker.columns.all(), pk=column_id)
    if column.is_fixed or not can_manage_tracker_columns(request.user, tracker):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    try:
        data = json.loads(request.body)
        column.label = data.get("label", column.label).strip()
        column.choices = data.get("choices", column.choices).strip()
        column.is_frozen = data.get("is_frozen", column.is_frozen)
        column.is_hidden = data.get("is_hidden", column.is_hidden)
        column.is_read_only = data.get("is_read_only", column.is_read_only)
        column.is_locked = data.get("is_locked", column.is_locked)
        column.required = data.get("required", column.required)
        column.unique = data.get("unique", column.unique)
        column.default_value = data.get("default_value", column.default_value).strip()
        column.permission_level = data.get("permission_level", column.permission_level)
        column.save()
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Error saving settings: {str(e)}"}, status=400)
        
    return JsonResponse({"status": "success", "column_id": column.id})


@login_required
@require_POST
def tracker_share(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    if not can_manage_trackers(request.user):
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    try:
        data = json.loads(request.body)
        user_ids = data.get("user_ids", [])
        dept_ids = data.get("department_ids", [])
        team_ids = data.get("team_ids", [])
    except Exception:
        return JsonResponse({"status": "error", "message": "Invalid JSON payload"}, status=400)
        
    tracker.shared_with_users.set(EmployeeUser.objects.filter(id__in=user_ids))
    from employee_management.models import Department, Team
    tracker.shared_with_departments.set(Department.objects.filter(id__in=dept_ids))
    tracker.shared_with_teams.set(Team.objects.filter(id__in=team_ids))
    
    return JsonResponse({"status": "success", "message": "Table access permissions updated."})


# ============================================================================
# TASK REVIEW WORKFLOW ENDPOINTS
# ============================================================================

@login_required
@require_POST
def task_mark_review(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    
    if request.user != task.assigned_to and request.user.role not in ("SUPER_ADMIN", "ADMIN"):
        return JsonResponse({"status": "error", "message": "Only the assigned employee can submit this task for review"}, status=403)
        
    task.status = "READY_FOR_REVIEW"
    task.save(update_fields=["status", "updated_at"])
    
    TaskHistory.objects.create(
        row=task,
        action="READY_FOR_REVIEW",
        field_name="status",
        old_value="IN_PROGRESS",
        new_value="READY_FOR_REVIEW",
        changed_by=request.user
    )
    Notification.objects.create(
        user=task.assigned_by,
        row=task,
        notif_type="ALERT",
        payload={"message": f"Task '{task.task_name}' is ready for review.", "task_name": task.task_name},
        sent_at=timezone.now()
    )
    return JsonResponse({"status": "success", "message": "Task submitted for review."})


@login_required
@require_POST
def task_review_decide(request, tracker_id, task_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    
    if not can_manage_tasks(request.user, tracker) and request.user != task.assigned_by:
        return JsonResponse({"status": "error", "message": "Only the assigner or admins can review this task"}, status=403)
        
    try:
        data = json.loads(request.body)
        decision = data.get("decision") # APPROVED, REJECTED, CHANGES_REQUESTED
        notes = data.get("notes", "").strip()
    except Exception:
        return JsonResponse({"status": "error", "message": "Invalid payload"}, status=400)
        
    old_status = task.status
    if decision == "APPROVED":
        task.status = "COMPLETED"
    elif decision == "REJECTED":
        task.status = "CANCELLED"
    elif decision == "CHANGES_REQUESTED":
        task.status = "IN_PROGRESS"
    else:
        return JsonResponse({"status": "error", "message": "Invalid review decision"}, status=400)
        
    task.save(update_fields=["status", "updated_at"])
    
    TaskHistory.objects.create(
        row=task,
        action=f"REVIEW_{decision}",
        field_name="status",
        old_value=old_status,
        new_value=task.status,
        changed_by=request.user,
        metadata={"notes": notes}
    )
    
    comment_content = f"Review Decision: **{decision}**.\n\nNotes: {notes if notes else 'None'}"
    TaskComment.objects.create(
        row=task,
        content=comment_content,
        created_by=request.user,
        internal=False
    )
    
    Notification.objects.create(
        user=task.assigned_to,
        row=task,
        notif_type="ALERT",
        payload={"message": f"Your task review was decided: {decision}.", "task_name": task.task_name},
        sent_at=timezone.now()
    )
    
    return JsonResponse({"status": "success", "message": f"Task review recorded: {decision}."})


@login_required
@require_POST
def comment_pin_toggle(request, tracker_id, task_id, comment_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    task = get_object_or_404(TaskRow, pk=task_id, tracker=tracker)
    comment = get_object_or_404(TaskComment, pk=comment_id, row=task)
    
    if not can_manage_tasks(request.user, tracker) and request.user != task.assigned_by:
        return JsonResponse({"status": "error", "message": "Permission denied"}, status=403)
        
    comment.is_pinned = not comment.is_pinned
    comment.save(update_fields=["is_pinned", "updated_at"])
    
    action = "pinned" if comment.is_pinned else "unpinned"
    return JsonResponse({"status": "success", "message": f"Comment has been {action}."})
