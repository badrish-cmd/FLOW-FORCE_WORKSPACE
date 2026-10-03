import datetime
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tables.models import Table, Row, CellValue
from tasks.models import Task, ActivityLog
from auth_app.models import EmployeeUser


class RowCreationValidationError(Exception):
    """Raised when row creation validation fails (e.g. invalid date format, missing mandatory date)."""
    pass


class RowService:
    """
    Dedicated service for row creation and associated cell/task synchronization.
    Extracted from tables/views.py in Phase 3B to isolate row-creation business logic.
    """

    @classmethod
    @transaction.atomic
    def create_row(cls, table, user, cells_data=None, assigned_to_ids=None):
        """
        Creates a new Row in the given Table, computes sequential S_NO with concurrency protection,
        populates system and custom CellValue records, creates the associated Task, and records ActivityLog.

        :param table: Table model instance (or table_id integer/string)
        :param user: User instance performing the creation
        :param cells_data: Dict of column_name -> value
        :param assigned_to_ids: Optional list of user IDs to assign the task to
        :return: Created Row instance
        :raises RowCreationValidationError: If date format is invalid or mandatory fields are missing
        """
        if not isinstance(table, Table):
            table = Table.objects.select_for_update().get(id=table)

        cells_data = cells_data or {}
        assigned_to_ids = assigned_to_ids or []

        is_sales = table.job_type == "SALES"
        is_list_pid = table.job_type == "LIST_PID"
        is_personal = table.job_type == "PERSONAL"
        is_logs = table.job_type == "LOGS"

        # Verify DUE_DATE/FOLLOW_UP_DATE/RETURN_DATE and TASK_NAME/CUSTOMER_NAME/TOOL_NAME are present
        if is_sales:
            due_date_str = cells_data.get("FOLLOW_UP_DATE")
            task_name = cells_data.get("CUSTOMER_NAME")
            date_field_name = "FOLLOW_UP_DATE"
            name_field_name = "CUSTOMER_NAME"
        elif is_list_pid:
            due_date_str = cells_data.get("DUE_DATE_FLOW_FORCE") or cells_data.get("DUE_DATE_CUSTOMER")
            task_name = cells_data.get("ENQUIRY_NO/QUOTATION_NO") or cells_data.get("ENQUIRY_NO") or cells_data.get("PID") or "Unnamed"
            date_field_name = "DUE_DATE_FLOW_FORCE"
            name_field_name = "ENQUIRY_NO/QUOTATION_NO"
        elif is_personal:
            due_date_str = None
            task_name = "Personal Task"
            date_field_name = "DUE_DATE"
            name_field_name = "TASK_NAME"
        elif is_logs:
            due_date_str = cells_data.get("RETURN_DATE") or cells_data.get("DUE_DATE") or cells_data.get("ISSUE_DATE") or cells_data.get("DATE") or timezone.localdate().isoformat()
            task_name = cells_data.get("TOOL_NAME") or cells_data.get("TASK_NAME")
            date_field_name = "RETURN_DATE"
            name_field_name = "TOOL_NAME"
        else:
            due_date_str = cells_data.get("DUE_DATE")
            task_name = cells_data.get("TASK_NAME")
            date_field_name = "DUE_DATE"
            name_field_name = "TASK_NAME"

        priority = cells_data.get("priority", "MEDIUM")

        due_date = None
        if due_date_str:
            try:
                due_date = datetime.datetime.strptime(str(due_date_str).split("T")[0], "%Y-%m-%d").date()
            except ValueError:
                raise RowCreationValidationError(f"Invalid {date_field_name} format. Use YYYY-MM-DD")
        elif not is_list_pid and not is_personal:
            raise RowCreationValidationError(f"{date_field_name} is mandatory")

        # 1. Create Row
        row = Row.objects.create(table=table, created_by=user)

        # Get system columns
        cols = {col.name: col for col in table.columns.all()}

        # 2. Compute S_NO with concurrency protection (select_for_update)
        latest_s_no = 0
        s_no_col = cols.get("S_NO")
        if s_no_col:
            latest_cell = CellValue.objects.select_for_update().filter(column=s_no_col).order_by("-id").first()
            if latest_cell and latest_cell.value is not None:
                try:
                    latest_s_no = int(latest_cell.value)
                except (ValueError, TypeError):
                    latest_s_no = 0
        s_no = latest_s_no + 1

        # Save CellValues
        if is_sales:
            cell_values = {
                "S_NO": s_no,
                "DATE": timezone.localdate().isoformat(),
                "FOLLOW_UP_DATE": due_date.isoformat() if due_date else None,
                "CUSTOMER_NAME": task_name,
                "INITIAL_MAIL": "NO",
                "ALERT_MAIL": "NO"
            }
        elif is_list_pid:
            enq_col_name = "ENQUIRY_NO/QUOTATION_NO" if "ENQUIRY_NO/QUOTATION_NO" in cols else "ENQUIRY_NO"
            cell_values = {
                "S_NO": s_no,
                "DATE": timezone.localdate().isoformat(),
                enq_col_name: task_name,
                "DUE_DATE_FLOW_FORCE": due_date.isoformat() if due_date else None,
                "INITIAL_MAIL": "NO",
                "ALERT_MAIL": "NO"
            }
        elif is_logs:
            issue_date_str = cells_data.get("ISSUE_DATE") or cells_data.get("DATE")
            issue_date_val = timezone.localdate().isoformat()
            if issue_date_str:
                try:
                    issue_date_val = datetime.datetime.strptime(str(issue_date_str).split("T")[0], "%Y-%m-%d").date().isoformat()
                except ValueError:
                    pass
            # Return date defaults to issue date if not provided
            return_date_val = due_date.isoformat() if due_date else issue_date_val
            status_val = cells_data.get("STATUS", "Not Returned")
            days_overdue = 0
            if str(status_val).strip().upper() not in ["RETURNED", "COMPLETED"]:
                try:
                    ret_d = datetime.datetime.strptime(return_date_val, "%Y-%m-%d").date()
                    if timezone.localdate() > ret_d:
                        days_overdue = (timezone.localdate() - ret_d).days
                except ValueError:
                    pass

            cell_values = {
                "S_NO": s_no,
                "ISSUE_DATE": issue_date_val,
                "RETURN_DATE": return_date_val,
                "DAYS_OVERDUE": days_overdue,
                "TOOL_NAME": task_name,
                "STATUS": status_val,
                "ISSUED_BY": cells_data.get("ISSUED_BY", getattr(user, "full_name", "") or getattr(user, "email", "")),
                "RECEIVED_BY": cells_data.get("RECEIVED_BY", ""),
                "INITIAL_MAIL": "NO",
                "ALERT_MAIL": "NO"
            }
        elif is_personal:
            cell_values = {}
        else:
            cell_values = {
                "S_NO": s_no,
                "DATE": timezone.localdate().isoformat(),
                "DUE_DATE": due_date.isoformat() if due_date else None,
                "TASK_NAME": task_name,
                "INITIAL_MAIL": "NO",
                "ALERT_MAIL": "NO"
            }

        # Merge custom columns input
        for key, val in cells_data.items():
            if key not in cell_values and key in cols:
                cell_values[key] = val

        if "PID" in cols and "PID" not in cell_values:
            cell_values["PID"] = ""

        for col_name, val in cell_values.items():
            col = cols.get(col_name)
            if col:
                CellValue.objects.update_or_create(
                    row=row,
                    column=col,
                    defaults={"value": val, "updated_by": user}
                )

        # 3. Create Task
        task = Task.objects.create(
            row=row,
            due_date=due_date,
            priority=priority,
            status="PENDING",
            assigned_by=user
        )

        # Look for any cell value belonging to a USER column or assignee column to set assignee
        user_to_assign = None
        for col_name, val in cell_values.items():
            col = cols.get(col_name)
            if col and (col.data_type == "USER" or col_name.upper() in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]):
                if val:
                    val_str = str(val).strip()
                    if val_str.isdigit():
                        user_to_assign = EmployeeUser.objects.filter(id=int(val_str), is_active=True).first()
                    elif "@" in val_str:
                        user_to_assign = EmployeeUser.objects.filter(email__iexact=val_str, is_active=True).first()
                    else:
                        user_to_assign = EmployeeUser.objects.filter(full_name__iexact=val_str, is_active=True).first()
                        if not user_to_assign:
                            user_to_assign = EmployeeUser.objects.filter(
                                Q(full_name__icontains=val_str) | Q(email__icontains=val_str),
                                is_active=True
                            ).first()
                    break

        # Handle assignments if provided
        if assigned_to_ids:
            employees = EmployeeUser.objects.filter(id__in=assigned_to_ids)
            task.assigned_to.set(employees)
        elif user_to_assign:
            task.assigned_to.set([user_to_assign])

        # Log creation
        try:
            ActivityLog.objects.create(
                task=task,
                action="Created Task Row",
                user=user,
                details={"task_name": str(task_name), "due_date": str(due_date_str) if due_date_str else ""}
            )
        except Exception:
            pass

        return row
