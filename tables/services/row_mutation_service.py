import datetime
from django.db import transaction
from django.db.models import Q

from tables.models import Table, Row, Column, CellValue
from tasks.models import Task, ActivityLog
from auth_app.models import EmployeeUser
from tables.permissions import has_table_access, get_column_access_level
from tables.services.cell_service import sync_logs_row_overdue


class RowMutationError(Exception):
    """Base exception for row mutation errors."""
    pass


class RowPermissionDeniedError(RowMutationError):
    """Raised when user lacks permission to edit row or table."""
    pass


class RowValidationError(RowMutationError):
    """Raised when row mutation validation fails."""
    pass


class RowMutationService:
    """
    Dedicated service for row edits, multi-cell row mutations,
    table-wide bulk updates, and associated task/log synchronization.
    Extracted from tables/views.py in Phase 3D.
    """

    @classmethod
    @transaction.atomic
    def edit_row(cls, row, cells_data, user, check_permissions=True):
        """
        Updates multiple cells for a given row, enforces permissions, and synchronizes
        associated Task state (due dates, status, assignees), mail columns, ActivityLog,
        and LOGS table overdue calculations.

        :param row: Row instance or row ID
        :param cells_data: Dictionary mapping column names to new values {"COL_NAME": value}
        :param user: User performing the update
        :param check_permissions: Boolean whether to enforce table and column level permissions
        :return: Tuple (row, updated_columns)
        :raises RowPermissionDeniedError: If user lacks table or row edit access
        """
        if not isinstance(row, Row):
            row = Row.objects.get(id=row)
        table = row.table

        task = getattr(row, "task", None)
        is_assigned = False
        if task:
            is_assigned = task.assigned_to.filter(id=user.id).exists()

        if check_permissions:
            if not (has_table_access(user, table, "EDIT") or is_assigned):
                raise RowPermissionDeniedError("No edit access to this table or task row")

        if cells_data is None:
            cells_data = {}

        cols = {col.name: col for col in table.columns.all()}

        updated_columns = []
        for col_name, value in cells_data.items():
            column = cols.get(col_name)
            if not column:
                continue

            # Enforce column level permissions
            if check_permissions and table.job_type != "LIST_PID":
                if is_assigned:
                    if column.name == "S_NO" or column.name in ["INITIAL_MAIL", "ALERT_MAIL"]:
                        continue
                else:
                    perm = get_column_access_level(user, column)
                    if perm != "EDITABLE":
                        continue

            # Update CellValue
            CellValue.objects.update_or_create(
                row=row, column=column,
                defaults={"value": value, "updated_by": user}
            )
            updated_columns.append(column.name)

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
                                pass

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

            # Sync with Task status
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

            # Sync with Task assigned_to if column data_type is USER or column name represents assignment
            if column.data_type == "USER" or col_name_upper in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]:
                task = getattr(row, "task", None)
                if task:
                    try:
                        if value:
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
                        if value:
                            assignee_user = EmployeeUser.objects.filter(
                                Q(full_name__icontains=value) | Q(email__icontains=value),
                                is_active=True
                            ).first()
                            if assignee_user:
                                task.assigned_to.set([assignee_user])
                                task.assigned_by = user
                                task.save()
                            else:
                                task.assigned_to.clear()
                        else:
                            task.assigned_to.clear()

        # Log change
        task = getattr(row, "task", None)
        if task and updated_columns:
            ActivityLog.objects.create(
                task=task,
                action="Updated multiple cells in row",
                user=user,
                details={"updated_columns": updated_columns}
            )

        if table.job_type == "LOGS":
            sync_logs_row_overdue(row, request_user=user)

        return row, updated_columns

    @classmethod
    @transaction.atomic
    def bulk_update_table(cls, table, field, value, user, check_permissions=True):
        """
        Updates a specific field across all non-archived rows of a table.
        Supported fields: "INITIAL_MAIL", "ALERT_MAIL", "STATUS".

        :param table: Table instance or table ID
        :param field: Field identifier string
        :param value: Value to set (or None)
        :param user: User performing the update
        :param check_permissions: Boolean whether to enforce table admin permissions
        :return: Number of rows updated
        :raises RowPermissionDeniedError: If user is not an admin on the table
        :raises RowValidationError: If field is not supported
        """
        if not isinstance(table, Table):
            table = Table.objects.get(id=table)

        if check_permissions:
            if not has_table_access(user, table, "ADMIN"):
                raise RowPermissionDeniedError("Only admins can perform bulk updates")

        if field not in ["INITIAL_MAIL", "ALERT_MAIL", "STATUS"]:
            raise RowValidationError("Invalid field for bulk update")

        rows = table.rows.filter(is_archived=False)
        updated_count = 0

        if field == "INITIAL_MAIL":
            col = table.columns.filter(name__iexact="INITIAL_MAIL").first()
            if col:
                for row in rows:
                    CellValue.objects.update_or_create(
                        row=row, column=col,
                        defaults={"value": "YES", "updated_by": user}
                    )
                    task = getattr(row, "task", None)
                    if task:
                        task.initial_mail_sent = True
                        task.save(update_fields=["initial_mail_sent"])
                    updated_count += 1
        elif field == "ALERT_MAIL":
            col = table.columns.filter(name__iexact="ALERT_MAIL").first()
            if col:
                for row in rows:
                    CellValue.objects.update_or_create(
                        row=row, column=col,
                        defaults={"value": "YES", "updated_by": user}
                    )
                    task = getattr(row, "task", None)
                    if task:
                        task.alert_mail_sent = True
                        task.save(update_fields=["alert_mail_sent"])
                    updated_count += 1
        elif field == "STATUS":
            status_col = table.columns.filter(name__iexact="STATUS").first()
            for row in rows:
                if status_col:
                    CellValue.objects.update_or_create(
                        row=row, column=status_col,
                        defaults={"value": "COMPLETED", "updated_by": user}
                    )
                task = getattr(row, "task", None)
                if task:
                    task.status = "COMPLETED"
                    task.save(update_fields=["status"])

                    ActivityLog.objects.create(
                        task=task,
                        action="Updated cell STATUS via Bulk Update",
                        user=user,
                        details={"column": "STATUS", "value": "COMPLETED"}
                    )
                updated_count += 1

        return updated_count
