import datetime
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tables.models import Table, Row, Column, CellValue
from tasks.models import Task, ActivityLog
from auth_app.models import EmployeeUser
from tables.permissions import has_table_access, get_column_access_level
from .broadcaster import TableEventBroadcaster


class CellMutationError(Exception):
    """Base exception for cell mutation errors."""
    pass


class CellPermissionDeniedError(CellMutationError):
    """Raised when user lacks permission to edit a cell or column."""
    pass


class CellValidationError(CellMutationError):
    """Raised when cell value validation fails (e.g. invalid date format)."""
    pass


def sync_logs_row_overdue(row, request_user=None):
    """
    For a LOGS table row:
    - Auto-captures RETURN_DATE to match ISSUE_DATE if missing.
    - Calculates and persists DAYS_OVERDUE based on RETURN_DATE and STATUS.
    """
    if not row or not row.table or row.table.job_type != "LOGS":
        return

    today = timezone.localdate()
    cols = {col.name.upper(): col for col in row.table.columns.all()}

    issue_col = cols.get("ISSUE_DATE") or cols.get("DATE")
    return_col = cols.get("RETURN_DATE") or cols.get("DUE_DATE")
    status_col = cols.get("STATUS")
    overdue_col = cols.get("DAYS_OVERDUE")

    if not overdue_col:
        overdue_col, _ = Column.objects.get_or_create(
            table=row.table,
            name="DAYS_OVERDUE",
            defaults={
                "data_type": "NUMBER",
                "is_mandatory": False,
                "is_system_column": True,
                "position": 4,
            },
        )

    issue_cell = CellValue.objects.filter(row=row, column=issue_col).first() if issue_col else None
    return_cell = CellValue.objects.filter(row=row, column=return_col).first() if return_col else None
    status_cell = CellValue.objects.filter(row=row, column=status_col).first() if status_col else None

    issue_date = None
    if issue_cell and issue_cell.value:
        try:
            issue_date = datetime.datetime.strptime(str(issue_cell.value).split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            pass

    return_date = None
    if return_cell and return_cell.value:
        try:
            return_date = datetime.datetime.strptime(str(return_cell.value).split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            pass

    # Auto system capture: RETURN_DATE has to be same as ISSUE_DATE if empty/missing
    if not return_date and issue_date:
        return_date = issue_date
        if return_col:
            CellValue.objects.update_or_create(
                row=row,
                column=return_col,
                defaults={"value": return_date.isoformat(), "updated_by": request_user},
            )

    status_val = str(status_cell.value).strip() if (status_cell and status_cell.value) else "Not Returned"
    is_returned = status_val.upper() in ["RETURNED", "COMPLETED"]

    days_overdue = 0
    if is_returned:
        ref_date = row.updated_at.date() if row.updated_at else today
        if return_date and ref_date > return_date:
            days_overdue = (ref_date - return_date).days
    else:
        if return_date and today > return_date:
            days_overdue = (today - return_date).days

    CellValue.objects.update_or_create(
        row=row,
        column=overdue_col,
        defaults={"value": days_overdue, "updated_by": request_user},
    )


class CellMutationService:
    """
    Dedicated service for cell mutation, validation, permissions, and side-effect synchronization.
    Extracted from tables/views.py in Phase 3C.
    """

    @classmethod
    @transaction.atomic
    def update_cell(cls, row, column, value, user, check_permissions=True):
        """
        Updates a single cell value, enforces permissions, and synchronizes associated task and log state.

        :param row: Row instance or row ID
        :param column: Column instance or column ID
        :param value: New cell value
        :param user: User performing the update
        :param check_permissions: Boolean whether to enforce table and column level permissions
        :return: Updated CellValue instance
        :raises CellPermissionDeniedError: If user lacks table or column edit access
        :raises CellValidationError: If date format validation fails
        """
        if not isinstance(row, Row):
            row = Row.objects.get(id=row)
        table = row.table

        if not isinstance(column, Column):
            column = Column.objects.get(id=column, table=table)

        task = getattr(row, "task", None)
        is_assigned = False
        if task:
            is_assigned = task.assigned_to.filter(id=user.id).exists()

        if check_permissions:
            if not (has_table_access(user, table, "EDIT") or is_assigned):
                raise CellPermissionDeniedError("No edit access to this table or task row")

            # Enforce column level permissions
            if table.job_type != "LIST_PID":
                if is_assigned:
                    if column.name == "S_NO" or column.name in ["INITIAL_MAIL", "ALERT_MAIL", "DAYS_OVERDUE"]:
                        raise CellPermissionDeniedError(f"Column {column.name} is read-only for assignees")
                else:
                    perm = get_column_access_level(user, column)
                    if perm != "EDITABLE":
                        raise CellPermissionDeniedError(f"Column {column.name} is read-only or hidden for you")

        # Update CellValue
        cell, created = CellValue.objects.update_or_create(
            row=row,
            column=column,
            defaults={"value": value, "updated_by": user},
        )

        # Sync System Columns with Task Model if necessary
        is_list_pid = (table.job_type == "LIST_PID")
        if column.is_system_column or (is_list_pid and column.name.upper() in ["DUE_DATE_FLOW_FORCE", "DUE_DATE_CUSTOMER"]):
            task = getattr(row, "task", None)
            if task:
                new_date = None
                if is_list_pid:
                    flow_force_col = Column.objects.filter(table=table, name__iexact="DUE_DATE_FLOW_FORCE").first()
                    customer_col = Column.objects.filter(table=table, name__iexact="DUE_DATE_CUSTOMER").first()

                    ff_val = CellValue.objects.filter(row=row, column=flow_force_col).first() if flow_force_col else None
                    cust_val = CellValue.objects.filter(row=row, column=customer_col).first() if customer_col else None

                    if ff_val and ff_val.value:
                        try:
                            new_date = datetime.datetime.strptime(str(ff_val.value).split("T")[0], "%Y-%m-%d").date()
                        except ValueError:
                            pass
                    if not new_date and cust_val and cust_val.value:
                        try:
                            new_date = datetime.datetime.strptime(str(cust_val.value).split("T")[0], "%Y-%m-%d").date()
                        except ValueError:
                            pass
                else:
                    if column.name in ["DUE_DATE", "FOLLOW_UP_DATE", "RETURN_DATE", "DUE_DATE_FLOW_FORCE", "DUE_DATE_CUSTOMER"]:
                        try:
                            new_date = datetime.datetime.strptime(str(value).split("T")[0], "%Y-%m-%d").date()
                        except ValueError:
                            raise CellValidationError("Invalid date format")

                if new_date:
                    if task.due_date != new_date:
                        task.due_date = new_date
                        task.alert_mail_sent = False
                        task.save(update_fields=["due_date", "alert_mail_sent"])
                        from tasks.tasks import update_task_row_mail_columns
                        update_task_row_mail_columns(task)
                    else:
                        task.due_date = new_date
                        task.save(update_fields=["due_date"])
                elif not is_list_pid and column.name in ["DUE_DATE", "FOLLOW_UP_DATE", "RETURN_DATE", "DUE_DATE_FLOW_FORCE", "DUE_DATE_CUSTOMER"]:
                    raise CellValidationError("Invalid date format")
            elif not is_list_pid:
                # No task but system column
                pass
        elif column.name in ["TASK_NAME", "CUSTOMER_NAME", "ENQUIRY_NO", "TOOL_NAME"] and column.is_system_column:
            # Activity log detail update
            pass

        # Sync with Task assigned_to if column data_type is USER or column name represents assignment
        col_name_upper = column.name.upper()
        if col_name_upper == "STATUS":
            task = getattr(row, "task", None)
            if task:
                val_upper = str(value).upper().strip().replace(" ", "_")
                if val_upper in ["COMPLETE", "COMPLETED", "RETURNED"]:
                    val_upper = "COMPLETED"
                elif val_upper in ["NOT_RETURNED", "NOT RETURNED", "PENDING"]:
                    val_upper = "PENDING"
                valid_statuses = [choice[0] for choice in Task.STATUS_CHOICES]
                if val_upper in valid_statuses:
                    task.status = val_upper
                    task.save(update_fields=["status"])

        if column.data_type == "USER" or col_name_upper in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]:
            task = getattr(row, "task", None)
            if task:
                try:
                    if value:
                        # Try parsing as ID first
                        if str(value).isdigit():
                            assignee_user = EmployeeUser.objects.get(id=int(value), is_active=True)
                        elif "@" in str(value):
                            assignee_user = EmployeeUser.objects.get(email=value, is_active=True)
                        else:
                            assignee_user = EmployeeUser.objects.get(full_name__iexact=value, is_active=True)

                        task.assigned_to.set([assignee_user])
                        task.assigned_by = user
                        task.save()
                    else:
                        task.assigned_to.clear()
                except EmployeeUser.DoesNotExist:
                    # Fallback to case-insensitive partial match on full_name/email
                    if value:
                        assignee_user = EmployeeUser.objects.filter(
                            Q(full_name__icontains=value) | Q(email__icontains=value),
                            is_active=True,
                        ).first()
                        if assignee_user:
                            task.assigned_to.set([assignee_user])
                            task.assigned_by = user
                            task.save()
                        else:
                            task.assigned_to.clear()

        # Log change
        task = getattr(row, "task", None)
        if task:
            ActivityLog.objects.create(
                task=task,
                action=f"Updated cell {column.name}",
                user=user,
                details={"column": column.name, "value": value},
            )

        if table.job_type == "LOGS":
            sync_logs_row_overdue(row, request_user=user)

        # Broadcast cell_updated and task sync events on transaction commit
        task_id = task.id if task else None
        TableEventBroadcaster.broadcast_cell_updated(
            table_id=table.id,
            row_id=row.id,
            column_id=column.id,
            column_name=column.name,
            value=value,
            task_id=task_id,
            user=user
        )

        if task:
            if col_name_upper == "STATUS":
                TableEventBroadcaster.broadcast_task_status_changed(
                    table_id=table.id,
                    task_id=task.id,
                    row_id=row.id,
                    new_status=task.status,
                    user=user
                )
            elif column.data_type == "USER" or col_name_upper in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]:
                assignees = [{"id": u.id, "name": u.full_name or u.email} for u in task.assigned_to.all()]
                TableEventBroadcaster.broadcast_task_reassigned(
                    table_id=table.id,
                    task_id=task.id,
                    row_id=row.id,
                    assignees=assignees,
                    user=user
                )
            elif column.is_system_column:
                TableEventBroadcaster.broadcast_task_updated(
                    table_id=table.id,
                    task_id=task.id,
                    row_id=row.id,
                    user=user
                )

        return cell
