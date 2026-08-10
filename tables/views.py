from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone
from datetime import datetime

from .models import Table, Column, Row, CellValue, TableAccess, ColumnAccess
from .serializers import (
    TableSerializer, ColumnSerializer, RowSerializer,
    CellValueSerializer, TableAccessSerializer, ColumnAccessSerializer
)
from .permissions import get_accessible_tables, has_table_access, get_column_access_level
from tasks.models import Task, ActivityLog
from auth_app.models import EmployeeUser

class TableViewSet(viewsets.ModelViewSet):
    serializer_class = TableSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return get_accessible_tables(self.request.user).select_related('department').prefetch_related('columns')

    def perform_create(self, serializer):
        # Automatically assign creator and department if admin/department admin
        dept = self.request.user.department if self.request.user.role in ["ADMIN", "DEPARTMENT_ADMIN"] else None
        serializer.save(created_by=self.request.user, department=dept)

    @action(detail=True, methods=["post"], url_path="share")
    def share_table(self, request, pk=None):
        table = self.get_object_or_404(pk)
        if not has_table_access(request.user, table, "ADMIN"):
            return Response({"error": "Only admins can share this table"}, status=status.HTTP_403_FORBIDDEN)

        user_id = request.data.get("user")
        dept_id = request.data.get("department")
        access_level = request.data.get("access_level", "VIEW")

        if not user_id and not dept_id:
            return Response({"error": "Must provide user or department"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            if user_id:
                user = get_object_or_404(EmployeeUser, id=user_id)
                access, created = TableAccess.objects.update_or_create(
                    table=table, user=user,
                    defaults={"access_level": access_level}
                )
            else:
                from employee_management.models import Department
                dept = get_object_or_404(Department, id=dept_id)
                access, created = TableAccess.objects.update_or_create(
                    table=table, department=dept,
                    defaults={"access_level": access_level}
                )
        return Response(TableAccessSerializer(access).data, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance, "ADMIN"):
            return Response({"error": "Only admins can edit this table"}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance, "ADMIN"):
            return Response({"error": "Only admins can edit this table"}, status=status.HTTP_403_FORBIDDEN)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance, "ADMIN"):
            return Response({"error": "Only admins can delete this table"}, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="duplicate")
    @transaction.atomic
    def duplicate_table(self, request, pk=None):
        table = self.get_object_or_404(pk)
        if not has_table_access(request.user, table, "ADMIN"):
            return Response({"error": "Only admins can duplicate this table"}, status=status.HTTP_403_FORBIDDEN)

        # Clone table metadata
        new_table = Table.objects.create(
            name=f"Copy of {table.name}",
            description=table.description,
            created_by=request.user,
            department=table.department,
            job_type=table.job_type
        )

        column_mapping = {}

        # Match system columns by name and copy options, position, is_mandatory, etc.
        for old_col in table.columns.filter(is_system_column=True):
            new_col = new_table.columns.filter(name=old_col.name).first()
            if new_col:
                new_col.options = old_col.options
                new_col.position = old_col.position
                new_col.is_mandatory = old_col.is_mandatory
                new_col.save()
                column_mapping[old_col.id] = new_col

        # Clone custom columns (excluding system columns as they are auto-created in save())
        for old_col in table.columns.filter(is_system_column=False):
            existing_col = new_table.columns.filter(name=old_col.name).first()
            if existing_col:
                existing_col.options = old_col.options
                existing_col.position = old_col.position
                existing_col.is_mandatory = old_col.is_mandatory
                existing_col.save()
                column_mapping[old_col.id] = existing_col
            else:
                new_col = Column.objects.create(
                    table=new_table,
                    name=old_col.name,
                    data_type=old_col.data_type,
                    is_mandatory=old_col.is_mandatory,
                    is_system_column=False,
                    position=old_col.position,
                    options=old_col.options
                )
                column_mapping[old_col.id] = new_col

        # Clone TableAccess
        for access in table.access_rules.all():
            TableAccess.objects.create(
                table=new_table,
                user=access.user,
                department=access.department,
                access_level=access.access_level
            )

        # Clone Rows, CellValues and Tasks
        for old_row in table.rows.all():
            new_row = Row.objects.create(
                table=new_table,
                created_by=request.user,
                is_archived=old_row.is_archived
            )
            
            # Copy cells
            for old_cell in old_row.cells.all():
                new_col = column_mapping.get(old_cell.column_id)
                if new_col:
                    CellValue.objects.create(
                        row=new_row,
                        column=new_col,
                        value=old_cell.value,
                        updated_by=request.user
                    )
            
            # Copy Task if it exists
            if hasattr(old_row, "task"):
                old_task = old_row.task
                new_task = Task.objects.create(
                    row=new_row,
                    assigned_by=old_task.assigned_by,
                    status=old_task.status,
                    due_date=old_task.due_date,
                    priority=old_task.priority,
                    initial_mail_sent=old_task.initial_mail_sent,
                    alert_mail_sent=old_task.alert_mail_sent,
                    last_escalation_level=old_task.last_escalation_level,
                    last_escalation_at=old_task.last_escalation_at
                )
                if old_task.assigned_to.exists():
                    new_task.assigned_to.set(old_task.assigned_to.all())

        return Response(TableSerializer(new_table).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="bulk-delete-rows")
    @transaction.atomic
    def bulk_delete_rows(self, request, pk=None):
        table = self.get_object_or_404(pk)
        if not has_table_access(request.user, table, "EDIT"):
            return Response({"error": "No edit access to this table"}, status=status.HTTP_403_FORBIDDEN)
        
        row_ids = request.data.get("row_ids")
        if row_ids is not None:
            if not isinstance(row_ids, list):
                return Response({"error": "row_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
            rows = Row.objects.filter(table=table, id__in=row_ids)
            count = rows.count()
            rows.delete()
            return Response({"message": f"Successfully deleted {count} rows"}, status=status.HTTP_200_OK)
        else:
            rows = Row.objects.filter(table=table)
            count = rows.count()
            rows.delete()
            return Response({"message": f"Successfully deleted {count} rows"}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="send-escalation")
    @transaction.atomic
    def send_manual_escalation(self, request, pk=None):
        table = self.get_object_or_404(pk)
        # Verify access: Admin or Super Admin role globally, or table access ADMIN
        if not (request.user.role in ["SUPER_ADMIN", "ADMIN"] or has_table_access(request.user, table, "ADMIN")):
            return Response({"error": "Only admins can trigger escalation emails"}, status=status.HTTP_403_FORBIDDEN)

        row_ids = request.data.get("row_ids")
        today = timezone.localdate()
        if row_ids is None:
            rows = Row.objects.filter(table=table)
            # Find all associated tasks that are overdue (due_date < today) and not completed/approved
            tasks = Task.objects.filter(row__in=rows, due_date__lt=today).exclude(status__in=['COMPLETED', 'APPROVED'])
        else:
            if not isinstance(row_ids, list):
                return Response({"error": "row_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
            rows = Row.objects.filter(table=table, id__in=row_ids)
            # When rows are explicitly selected, we do not require the tasks to be overdue. We send for all selected tasks that are not completed/approved.
            tasks = Task.objects.filter(row__in=rows).exclude(status__in=['COMPLETED', 'APPROVED'])

        from django.template.loader import render_to_string
        from tasks.models import EmailLog
        from tasks.tasks import send_email_log_task
        from django.conf import settings

        sent_count = 0
        for task in tasks:
            if task.due_date:
                days_overdue = (today - task.due_date).days
                if days_overdue < 0:
                    days_overdue = 0
            else:
                days_overdue = 0

            recipients = list(task.assigned_to.all())
            unique_recipients = []
            seen_emails = set()
            for r in recipients:
                if r.email and r.email not in seen_emails:
                    seen_emails.add(r.email)
                    unique_recipients.append(r)

            if unique_recipients:
                task.last_escalation_level = days_overdue
                task.last_escalation_at = timezone.now()
                task.save()

                for recipient in unique_recipients:
                    if days_overdue > 0:
                        subject = f"ESCALATION: Overdue Task - {task.task_name} ({days_overdue} days overdue)"
                        intro_html = f"A task is <strong>{days_overdue}</strong> days overdue and requires immediate attention."
                    else:
                        subject = f"ESCALATION: Task Escalated - {task.task_name}"
                        intro_html = "A task has been escalated and requires immediate attention."

                    site_url = getattr(settings, 'SITE_URL', 'https://flowforceworkspace.cloud')
                    task_link = f"{site_url}/tables/{task.row.table_id}/?open_task_id={task.id}"

                    context = {
                        'recipient_name': recipient.full_name,
                        'days': days_overdue,
                        'intro_html': intro_html,
                        'task_name': task.task_name,
                        'due_date': str(task.due_date) if task.due_date else "Not Set",
                        'employee_name': ", ".join([u.full_name for u in task.assigned_to.all()]),
                        'department_name': task.row.table.department.name if task.row.table.department else "Global",
                        'status': task.status,
                        'priority': task.priority,
                        'task_link': task_link,
                        'pid_data': task.pid_data,
                        'customer_name_data': task.customer_name_data,
                        'task_name_data': task.task_name_data,
                    }

                    html_message = render_to_string('emails/overdue_escalation_mail.html', context)

                    email_log = EmailLog.objects.create(
                        recipient_email=recipient.email,
                        subject=subject,
                        body=html_message,
                        task=task,
                        email_type='OVERDUE_ESCALATION_MAIL',
                        status='PENDING',
                        max_retries=3,
                    )
                    send_email_log_task.delay(email_log.id)
                    sent_count += 1

        return Response({"message": f"Successfully sent escalation emails to {sent_count} recipients"}, status=status.HTTP_200_OK)

    def safe_parse_date(self, val):
        if val is None or val == "":
            return None
        import datetime as dt_mod
        if isinstance(val, (datetime, dt_mod.date)):
            return val if isinstance(val, dt_mod.date) else val.date()

        val_str = str(val).strip()
        if not val_str:
            return None

        # Check for Excel serial date numbers (e.g. 45443 or 45443.0) or 8-digit numeric dates
        try:
            val_float = float(val_str)
            if 10000 <= val_float <= 100000:
                base_date = dt_mod.date(1899, 12, 30)
                return base_date + dt_mod.timedelta(days=int(val_float))
            elif val_str.isdigit() and len(val_str) == 8:
                try:
                    return dt_mod.date(int(val_str[:4]), int(val_str[4:6]), int(val_str[6:8]))
                except ValueError:
                    pass
                try:
                    return dt_mod.date(int(val_str[4:8]), int(val_str[2:4]), int(val_str[:2]))
                except ValueError:
                    pass
        except ValueError:
            pass

        # Try Django's parse_date first
        from django.utils.dateparse import parse_date
        try:
            d = parse_date(val_str)
            if d:
                return d
        except Exception:
            pass

        # Try dateutil parser with dayfirst=True and dayfirst=False
        from dateutil import parser as du_parser
        try:
            return du_parser.parse(val_str, dayfirst=True).date()
        except Exception:
            pass

        try:
            return du_parser.parse(val_str, dayfirst=False).date()
        except Exception:
            pass

        # Try explicit format strptime
        for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
            try:
                return dt_mod.datetime.strptime(val_str, fmt).date()
            except ValueError:
                pass

        return None

    def _import_rows_from_csv_data(self, file_data, table, request_user, header_row=None, data_row=None):
        import csv
        import io
        from django.utils import timezone

        lines = file_data.splitlines()
        
        # 1. Dynamically locate the header row
        header_idx = -1
        is_sales = table.job_type == "SALES"
        is_list_pid = table.job_type == "LIST_PID"
        is_personal = table.job_type == "PERSONAL"

        table_col_names_upper = {c.name.strip().upper() for c in table.columns.all()}
        
        if header_row is not None and data_row is not None:
            try:
                header_line = lines[int(header_row) - 1]
                data_lines = lines[int(data_row) - 1:]
                lines_to_parse = [header_line] + data_lines
            except IndexError:
                return None, f"Specified header row ({header_row}) or data row ({data_row}) is out of bounds."
        else:
            # Scan lines to find a header candidate
            for idx, line in enumerate(lines[:30]):
                if not line.strip():
                    continue
                tokens = [t.strip().upper() for t in line.replace(";", ",").replace("\t", ",").split(",")]
                tokens = [t for t in tokens if t]
                if not tokens:
                    continue

                has_s_no = any(t in ["S_NO", "S.NO", "S. NO.", "SL_NO", "SL.NO", "SL. NO.", "S NO", "SL NO", "SR_NO", "SR.NO"] for t in tokens)
                if is_sales:
                    has_task = any("CUSTOMER" in t or "CLIENT" in t or "TASK" in t for t in tokens)
                    has_due = any("FOLLOW" in t or "UP" in t or "DUE" in t for t in tokens)
                elif is_list_pid:
                    has_task = any("ENQUIRY" in t or "PID" in t or "TASK" in t or "QUOTATION" in t for t in tokens)
                    has_due = any("FLOW" in t or "FORCE" in t or "CUSTOMER" in t or "DUE" in t for t in tokens)
                else:
                    has_task = any("TASK" in t or "NAME" in t or "TITLE" in t or "SUBJECT" in t or "ITEM" in t for t in tokens)
                    has_due = any("DUE" in t or "DATE" in t or "FOLLOW" in t for t in tokens)

                col_match_count = sum(1 for t in tokens if t in table_col_names_upper or t.replace(" ", "_") in table_col_names_upper)

                if (has_s_no and (has_task or has_due)) or (has_task and has_due) or col_match_count >= 2:
                    header_idx = idx
                    break

            if header_idx != -1:
                lines_to_parse = lines[header_idx:]
            else:
                # Default to line 0 (Row 1 is header) if auto-detection finds no specific line
                lines_to_parse = lines

        # Detect delimiter (comma, semicolon, tab)
        sample_header = lines_to_parse[0] if lines_to_parse else ""
        delimiter = ","
        if ";" in sample_header and "," not in sample_header:
            delimiter = ";"
        elif "\t" in sample_header and "," not in sample_header:
            delimiter = "\t"

        io_string = io.StringIO("\n".join(lines_to_parse))
        reader = csv.DictReader(io_string, delimiter=delimiter)

        if not reader.fieldnames:
            return None, "Import file is empty or invalid"

        # Prepare helper to normalize names
        def normalize_header(name):
            if not name:
                return ""
            h = name.strip().upper()
            h = h.replace(".", "_").replace(" ", "_").replace("-", "_").replace("/", "_")
            while "__" in h:
                h = h.replace("__", "_")
            h = h.strip("_")
            
            # Map standard column name synonyms
            if is_sales:
                if h in ["TASK_NAME", "TASKNAME", "TASK", "CUSTOMER_NAME", "CUSTOMERNAME", "CUSTOMER", "CLIENT_NAME", "CLIENTNAME", "CLIENT", "NAME", "COMPANY"]:
                    return "CUSTOMER_NAME"
                if h in ["DUE_DATE", "DUEDATE", "FOLLOW_UP_DATE", "FOLLOWUPDATE", "FOLLOW_UP", "FOLLOWUP", "DATE"]:
                    return "FOLLOW_UP_DATE"
            elif is_list_pid:
                if h in ["ENQUIRY_NO", "ENQUIRYNO", "ENQUIRY", "ENQUIRIES", "TASK_NAME", "TASKNAME", "TASK", "CUSTOMER_NAME", "ENQUIRY_NO_QUOTATION_NO", "ENQUIRY_QUOTATION_NO", "ENQUIRY_NO_QUOTATION", "QUOTATION_NO", "QUOTATION", "QUOTATION_NUMBER", "ENQUIRY_NUMBER"]:
                    return "ENQUIRY_NO_QUOTATION_NO"
                if h in ["PID", "PID_NO", "PID_NUMBER"]:
                    return "PID"
                if h in ["DUE_DATE_FLOW_FORCE", "FLOW_FORCE_DUE_DATE", "FLOW_FORCE", "DUE_DATE", "FOLLOW_UP_DATE", "DUE", "DEADLINE"]:
                    return "DUE_DATE_FLOW_FORCE"
                if h in ["NEW_PID_NO", "NEWPIDNO", "NEW_PID"]:
                    return "NEW_PID_NO"
                if h in ["FFE_SINGAPORE", "FFE_SINGAPORE_PTE_LTD", "FFE"]:
                    return "FFE_SINGAPORE"
                if h in ["COMPANY_NAME", "COMPANYNAME", "COMPANY"]:
                    return "COMPANY_NAME"
                if h in ["DUE_DATE_CUSTOMER", "CUSTOMER_DUE_DATE", "DUE_CUSTOMER", "CUSTOMER_DUE"]:
                    return "DUE_DATE_CUSTOMER"
                if h in ["QTY", "QUANTITY"]:
                    return "QTY"
            else:
                if h in ["TASK_NAME", "TASKNAME", "TASK", "CUSTOMER_NAME", "CUSTOMERNAME", "CUSTOMER", "CLIENT_NAME", "CLIENTNAME", "CLIENT", "NAME", "TITLE", "SUBJECT", "DESCRIPTION", "PARTICULARS", "ITEM", "SUMMARY", "JOB", "JOB_NAME", "WORK"]:
                    return "TASK_NAME"
                if h in ["DUE_DATE", "DUEDATE", "FOLLOW_UP_DATE", "FOLLOWUPDATE", "FOLLOW_UP", "FOLLOWUP", "TARGET_DATE", "DEADLINE", "DUE", "DATE"]:
                    return "DUE_DATE"
            if h in ["INITIAL_MAIL", "INITIALMAIL"]:
                return "INITIAL_MAIL"
            if h in ["ALERT_MAIL", "ALERTMAIL"]:
                return "ALERT_MAIL"
            if h in ["S_NO", "SNO", "SL_NO", "SLNO", "SERIAL_NO", "SERIALNO", "S_NO_", "SR_NO", "SRNO", "NO", "SR"]:
                return "S_NO"
            return h

        # Map normalized DB column name -> Column object
        normalized_db_cols = {}
        for col in table.columns.all():
            norm_name = normalize_header(col.name)
            normalized_db_cols[norm_name] = col

        db_col_names = set(normalized_db_cols.keys())

        # Normalize CSV fieldnames to match DB columns
        csv_headers = []
        header_mapping = {}
        for name in reader.fieldnames:
            if not name:
                continue
            normalized = normalize_header(name)
            csv_headers.append(normalized)
            header_mapping[name] = normalized

        # Dynamic primary column matching
        if not is_personal:
            required_header = "CUSTOMER_NAME" if is_sales else ("ENQUIRY_NO_QUOTATION_NO" if is_list_pid else "TASK_NAME")
            if required_header not in csv_headers:
                non_task_sys_cols = {"S_NO", "DATE", "DUE_DATE", "FOLLOW_UP_DATE", "DUE_DATE_FLOW_FORCE", "DUE_DATE_CUSTOMER", "INITIAL_MAIL", "ALERT_MAIL"}
                candidate_primary = [h for h in csv_headers if h not in non_task_sys_cols]
                if not candidate_primary:
                    return None, f"Required column for task name/customer name/enquiry/quotation no is missing in the CSV sheet headers. Expected one of: {required_header}"

        # Performance Optimizations: pre-map columns, pre-query S_NO base, and setup user cache
        columns_by_name = {c.name: c for c in table.columns.all()}
        
        base_s_no = 0
        s_no_col = normalized_db_cols.get("S_NO")
        if s_no_col:
            latest_cell = CellValue.objects.filter(column=s_no_col).order_by("-id").first()
            if latest_cell and isinstance(latest_cell.value, int):
                base_s_no = latest_cell.value
        
        resolved_users_cache = {}
        list_pid_employees = []
        if is_list_pid:
            from tables.permissions import get_employees_with_table_access
            list_pid_employees = list(get_employees_with_table_access(table))
        
        rows_to_create = []
        row_temp_data = []
        row_import_idx = 1
        
        for row_dict in reader:
            normalized_row = {}
            has_any_value = False
            for original_key, val in row_dict.items():
                if not original_key:
                    continue
                normalized_key = header_mapping.get(original_key)
                if normalized_key:
                    normalized_row[normalized_key] = val
                    if val is not None and str(val).strip() != "":
                        has_any_value = True

            # Skip completely empty rows
            if not has_any_value:
                continue

            if is_list_pid:
                task_name = normalized_row.get("ENQUIRY_NO_QUOTATION_NO") or normalized_row.get("PID")
                if not task_name:
                    for k, v in normalized_row.items():
                        if k not in ["S_NO", "INITIAL_MAIL", "ALERT_MAIL"] and v and str(v).strip():
                            task_name = str(v).strip()
                            break
                if not task_name:
                    task_name = f"Row {row_import_idx}"

                ff_date_str = normalized_row.get("DUE_DATE_FLOW_FORCE")
                cust_date_str = normalized_row.get("DUE_DATE_CUSTOMER")
                ff_date = self.safe_parse_date(ff_date_str) if ff_date_str else None
                cust_date = self.safe_parse_date(cust_date_str) if cust_date_str else None
                due_date = ff_date or cust_date
            elif is_personal:
                task_name = None
                for col_name, val in normalized_row.items():
                    if col_name in ["TASK_NAME", "TASK NAME", "NAME", "TITLE", "SUBJECT", "TASK"]:
                        task_name = val
                        break
                if not task_name:
                    for col_name, val in normalized_row.items():
                        col = normalized_db_cols.get(col_name)
                        if col and col.data_type == "TEXT" and val:
                            task_name = val
                            break
                if not task_name:
                    task_name = f"Personal Row {row_import_idx}"
                due_date = None
            else:
                if is_sales:
                    task_name = normalized_row.get("CUSTOMER_NAME")
                    due_date_str = normalized_row.get("FOLLOW_UP_DATE")
                else:
                    task_name = normalized_row.get("TASK_NAME")
                    due_date_str = normalized_row.get("DUE_DATE")

                if not task_name:
                    for k, v in normalized_row.items():
                        if k not in ["S_NO", "INITIAL_MAIL", "ALERT_MAIL"] and v and str(v).strip():
                            task_name = str(v).strip()
                            break
                if not task_name:
                    task_name = f"Row {row_import_idx}"

                due_date = self.safe_parse_date(due_date_str) if due_date_str else None
            
            # Support lowercase/mixed-case options
            priority = "MEDIUM"
            for k, v in normalized_row.items():
                if k == "PRIORITY" and v:
                    priority = v
                    break
            
            status_val = "PENDING"
            for k, v in normalized_row.items():
                if k == "STATUS" and v:
                    status_val = v
                    break

            # Stage Row object creation (unsaved)
            row = Row(table=table, created_by=request_user)
            rows_to_create.append(row)

            # Auto compute S_NO (using pre-fetched base)
            s_no = base_s_no + row_import_idx
            row_import_idx += 1

            # Parse and normalize other system fields
            csv_date_str = normalized_row.get("DATE")
            date_val = None
            if csv_date_str:
                parsed_d = self.safe_parse_date(csv_date_str)
                if parsed_d:
                    date_val = parsed_d.isoformat()
            if not date_val and not is_list_pid:
                date_val = timezone.localdate().isoformat()

            initial_mail_val = normalized_row.get("INITIAL_MAIL", "NO")
            if initial_mail_val:
                initial_mail_val = str(initial_mail_val).strip().upper()
                if initial_mail_val not in ["YES", "NO"]:
                    initial_mail_val = "NO"
            else:
                initial_mail_val = "NO"

            alert_mail_val = normalized_row.get("ALERT_MAIL", "NO")
            if alert_mail_val:
                alert_mail_val = str(alert_mail_val).strip().upper()
                if alert_mail_val not in ["YES", "NO"]:
                    alert_mail_val = "NO"
            else:
                alert_mail_val = "NO"

            if is_sales:
                system_field_names = ["S_NO", "DATE", "FOLLOW_UP_DATE", "CUSTOMER_NAME", "INITIAL_MAIL", "ALERT_MAIL"]
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "FOLLOW_UP_DATE": due_date.isoformat() if due_date else None,
                    "CUSTOMER_NAME": task_name,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }
            elif is_list_pid:
                system_field_names = ["S_NO", "DATE", "ENQUIRY_NO_QUOTATION_NO", "DUE_DATE_FLOW_FORCE", "INITIAL_MAIL", "ALERT_MAIL"]
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "ENQUIRY_NO_QUOTATION_NO": task_name,
                    "DUE_DATE_FLOW_FORCE": ff_date.isoformat() if ff_date else None,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }
            elif is_personal:
                system_field_names = []
                cell_values = {}
            else:
                system_field_names = ["S_NO", "DATE", "DUE_DATE", "TASK_NAME", "INITIAL_MAIL", "ALERT_MAIL"]
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "DUE_DATE": due_date.isoformat() if due_date else None,
                    "TASK_NAME": task_name,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }

            # Map remaining custom columns including status, priority, and date columns
            for col_name, val in normalized_row.items():
                if col_name not in system_field_names:
                    if col_name in db_col_names:
                        col = normalized_db_cols[col_name]
                        if is_list_pid and col.name == "DUE_DATE_CUSTOMER":
                            cell_values[col.name] = cust_date.isoformat() if cust_date else None
                        else:
                            cell_values[col.name] = val

            # Normalize priority and status for Task model
            norm_priority = str(priority).upper().strip().replace(" ", "_")
            if norm_priority not in [choice[0] for choice in Task.PRIORITY_CHOICES]:
                norm_priority = "MEDIUM"

            # Resolve assignee user if provided in any USER data_type column or header representation
            user_to_assign = None
            from django.db.models import Q
            for col_name, val in cell_values.items():
                col = None
                if col_name in system_field_names:
                    col = normalized_db_cols.get(col_name)
                else:
                    col = columns_by_name.get(col_name)

                if col and (col.data_type == "USER" or col.name.upper() in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]):
                    if val:
                        cache_key = str(val).strip()
                        if cache_key in resolved_users_cache:
                            user_to_assign = resolved_users_cache[cache_key]
                        else:
                            try:
                                if str(val).isdigit():
                                    user_to_assign = EmployeeUser.objects.get(id=int(val), is_active=True)
                                elif "@" in str(val):
                                    user_to_assign = EmployeeUser.objects.get(email=val, is_active=True)
                                else:
                                    user_to_assign = EmployeeUser.objects.get(full_name__iexact=val, is_active=True)
                            except EmployeeUser.DoesNotExist:
                                user_to_assign = EmployeeUser.objects.filter(
                                    Q(full_name__icontains=val) | Q(email__icontains=val),
                                    is_active=True
                                ).first()
                            resolved_users_cache[cache_key] = user_to_assign
                        break

            row_temp_data.append({
                'cell_values': cell_values,
                'due_date': due_date,
                'norm_priority': norm_priority,
                'initial_mail_val': initial_mail_val,
                'alert_mail_val': alert_mail_val,
                'user_to_assign': user_to_assign,
                'system_field_names': system_field_names
            })

        # 1. Bulk Create Row records
        created_rows = Row.objects.bulk_create(rows_to_create)

        cells_to_create = []
        tasks_to_create = []

        # 2. Iterate through newly created rows to build unsaved CellValue and Task objects
        for i, row in enumerate(created_rows):
            temp = row_temp_data[i]
            cell_values = temp['cell_values']
            system_field_names = temp['system_field_names']

            for name, val in cell_values.items():
                col = normalized_db_cols.get(name) or columns_by_name.get(name)
                if col:
                    cells_to_create.append(
                        CellValue(row=row, column=col, value=val, updated_by=request_user)
                    )

            task = Task(
                row=row,
                due_date=temp['due_date'],
                priority=temp['norm_priority'],
                status="PENDING",
                assigned_by=request_user,
                initial_mail_sent=(temp['initial_mail_val'] == "YES"),
                alert_mail_sent=(temp['alert_mail_val'] == "YES")
            )
            tasks_to_create.append(task)

        # 3. Bulk insert CellValue and Task objects
        CellValue.objects.bulk_create(cells_to_create, batch_size=5000)
        created_tasks = Task.objects.bulk_create(tasks_to_create)

        # 4. Map task assignees in bulk using join table ThroughModel
        ThroughModel = Task.assigned_to.through
        through_fields = ThroughModel._meta.fields
        task_field = None
        user_field = None
        for f in through_fields:
            if f.is_relation:
                if f.related_model == Task:
                    task_field = f.name
                else:
                    user_field = f.name

        through_objs = []
        for i, task in enumerate(created_tasks):
            temp = row_temp_data[i]
            user_to_assign = temp['user_to_assign']
            assignees = [user_to_assign] if user_to_assign else []

            if is_list_pid:
                assignees = list_pid_employees

            for employee in assignees:
                kwargs = {
                    task_field: task,
                    user_field: employee
                }
                through_objs.append(ThroughModel(**kwargs))

        if through_objs:
            ThroughModel.objects.bulk_create(through_objs, batch_size=5000, ignore_conflicts=True)

        # After successfully importing and creating rows, analyze and setup filter for LIST_PID
        if is_list_pid:
            company_col = table.columns.filter(name__iexact="COMPANY_NAME").first()
            if company_col:
                unique_values = CellValue.objects.filter(
                    column=company_col,
                    row__table=table,
                    row__is_archived=False
                ).exclude(
                    value__isnull=True
                ).exclude(
                    value=""
                ).values_list("value", flat=True).distinct()
                
                cleaned_values = sorted(list(set(str(v).strip() for v in unique_values if str(v).strip())))
                
                company_col.data_type = "DROPDOWN"
                company_col.is_filterable = True
                company_col.options = ",".join(cleaned_values)
                company_col.save()

        return created_rows, None

    @action(detail=True, methods=["post"], url_path="import-csv")
    @transaction.atomic
    def import_csv(self, request, pk=None):
        try:
            table = self.get_object_or_404(pk)
            if not has_table_access(request.user, table, "EDIT"):
                return Response({"error": "No edit access to this table"}, status=status.HTTP_403_FORBIDDEN)

            csv_file = request.FILES.get("file")
            if not csv_file:
                return Response({"error": "No CSV file provided"}, status=status.HTTP_400_BAD_REQUEST)

            try:
                file_data = csv_file.read().decode("utf-8")
            except Exception:
                return Response({"error": "Failed to decode CSV file. Make sure it is encoded in UTF-8."}, status=status.HTTP_400_BAD_REQUEST)

            header_row = request.data.get("header_row") or request.POST.get("header_row")
            data_row = request.data.get("data_row") or request.POST.get("data_row")
            try:
                header_row = int(header_row) if header_row else None
                data_row = int(data_row) if data_row else None
            except ValueError:
                return Response({"error": "header_row and data_row must be integers"}, status=status.HTTP_400_BAD_REQUEST)

            created_rows, err = self._import_rows_from_csv_data(
                file_data, table, request.user, header_row=header_row, data_row=data_row
            )
            if err:
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

            return Response({"message": f"Successfully imported {len(created_rows)} rows"}, status=status.HTTP_201_CREATED)
        except Exception as e:
            import traceback
            return Response({
                "error": f"Internal Server Error during CSV import: {str(e)}",
                "traceback": traceback.format_exc()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="import-google-sheet")
    @transaction.atomic
    def import_google_sheet(self, request, pk=None):
        try:
            table = self.get_object_or_404(pk)
            if not has_table_access(request.user, table, "EDIT"):
                return Response({"error": "No edit access to this table"}, status=status.HTTP_403_FORBIDDEN)

            sheet_url = request.data.get("url")
            if not sheet_url:
                return Response({"error": "No Google Sheet URL provided"}, status=status.HTTP_400_BAD_REQUEST)

            import re
            match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
            if not match:
                return Response({"error": "Invalid Google Sheets URL format. Make sure it contains '/spreadsheets/d/[ID]'"}, status=status.HTTP_400_BAD_REQUEST)

            spreadsheet_id = match.group(1)
            gid_match = re.search(r"[#&?]gid=([0-9]+)", sheet_url)
            if gid_match:
                export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid_match.group(1)}"
            else:
                export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"

            import urllib.request
            try:
                req = urllib.request.Request(
                    export_url,
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                with urllib.request.urlopen(req, timeout=15) as response:
                    content = response.read().decode('utf-8')
            except Exception as e:
                return Response({"error": f"Error fetching Google Sheet: {str(e)}. Ensure the spreadsheet is public or shared 'Anyone with the link can view'."}, status=status.HTTP_400_BAD_REQUEST)

            header_row = request.data.get("header_row")
            data_row = request.data.get("data_row")
            try:
                header_row = int(header_row) if header_row else None
                data_row = int(data_row) if data_row else None
            except ValueError:
                return Response({"error": "header_row and data_row must be integers"}, status=status.HTTP_400_BAD_REQUEST)

            created_rows, err = self._import_rows_from_csv_data(
                content, table, request.user, header_row=header_row, data_row=data_row
            )
            if err:
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

            return Response({"message": f"Successfully imported {len(created_rows)} rows from Google Sheets"}, status=status.HTTP_201_CREATED)
        except Exception as e:
            import traceback
            return Response({
                "error": f"Internal Server Error during Google Sheet import: {str(e)}",
                "traceback": traceback.format_exc()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="bulk-update")
    @transaction.atomic
    def bulk_update(self, request, pk=None):
        table = self.get_object_or_404(pk)
        if not has_table_access(request.user, table, "ADMIN"):
            return Response({"error": "Only admins can perform bulk updates"}, status=status.HTTP_403_FORBIDDEN)

        field = request.data.get("field")
        value = request.data.get("value")

        if field not in ["INITIAL_MAIL", "ALERT_MAIL", "STATUS"]:
            return Response({"error": "Invalid field for bulk update"}, status=status.HTTP_400_BAD_REQUEST)

        rows = table.rows.filter(is_archived=False)
        updated_count = 0

        if field == "INITIAL_MAIL":
            col = table.columns.filter(name__iexact="INITIAL_MAIL").first()
            if col:
                for row in rows:
                    CellValue.objects.update_or_create(
                        row=row, column=col,
                        defaults={"value": "YES", "updated_by": request.user}
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
                        defaults={"value": "YES", "updated_by": request.user}
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
                        defaults={"value": "COMPLETED", "updated_by": request.user}
                    )
                task = getattr(row, "task", None)
                if task:
                    task.status = "COMPLETED"
                    task.save(update_fields=["status"])
                    
                    ActivityLog.objects.create(
                        task=task,
                        action="Updated cell STATUS via Bulk Update",
                        user=request.user,
                        details={"column": "STATUS", "value": "COMPLETED"}
                    )
                updated_count += 1

        return Response({"message": f"Successfully updated {updated_count} rows"}, status=status.HTTP_200_OK)

    def get_object_or_404(self, pk):
        obj = get_object_or_404(Table, pk=pk)
        self.check_object_permissions(self.request, obj)
        return obj

class ColumnViewSet(viewsets.ModelViewSet):
    serializer_class = ColumnSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        table_id = self.request.query_params.get("table")
        if not table_id:
            if self.action in ["retrieve", "update", "partial_update", "destroy", "clear_values", "delete_rows"]:
                from .permissions import get_accessible_tables
                accessible_tables = get_accessible_tables(self.request.user)
                return Column.objects.filter(table__in=accessible_tables)
            return Column.objects.none()
        table = get_object_or_404(Table, id=table_id)
        if not has_table_access(self.request.user, table, "VIEW"):
            return Column.objects.none()
        return Column.objects.filter(table=table)

    def create(self, request, *args, **kwargs):
        table_id = request.data.get("table")
        table = get_object_or_404(Table, id=table_id)
        if not has_table_access(request.user, table, "ADMIN"):
            return Response({"error": "Only admins can add columns"}, status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance.table, "ADMIN"):
            return Response({"error": "Only admins can update columns"}, status=status.HTTP_403_FORBIDDEN)
        if instance.is_system_column:
            if request.data.get("name") and request.data.get("name") != instance.name:
                return Response({"error": "Cannot rename system columns"}, status=status.HTTP_400_BAD_REQUEST)
            if request.data.get("data_type") and request.data.get("data_type") != instance.data_type:
                if instance.name not in ("TASK_NAME", "CUSTOMER_NAME"):
                    return Response({"error": "Cannot change data type of system columns"}, status=status.HTTP_400_BAD_REQUEST)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance.table, "ADMIN"):
            return Response({"error": "Only admins can update columns"}, status=status.HTTP_403_FORBIDDEN)
        if instance.is_system_column:
            if request.data.get("name") and request.data.get("name") != instance.name:
                return Response({"error": "Cannot rename system columns"}, status=status.HTTP_400_BAD_REQUEST)
            if request.data.get("data_type") and request.data.get("data_type") != instance.data_type:
                if instance.name not in ("TASK_NAME", "CUSTOMER_NAME"):
                    return Response({"error": "Cannot change data type of system columns"}, status=status.HTTP_400_BAD_REQUEST)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance.table, "ADMIN"):
            return Response({"error": "Only admins can delete columns"}, status=status.HTTP_403_FORBIDDEN)
        if instance.is_system_column:
            return Response({"error": "Cannot delete system columns"}, status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="clear-values")
    @transaction.atomic
    def clear_values(self, request, pk=None):
        column = self.get_object()
        if not has_table_access(request.user, column.table, "EDIT"):
            return Response({"error": "No edit access to this table"}, status=status.HTTP_403_FORBIDDEN)
        if column.is_system_column:
            return Response({"error": "Cannot clear system columns"}, status=status.HTTP_400_BAD_REQUEST)
        
        CellValue.objects.filter(column=column).update(value=None)
        return Response({"message": f"Successfully cleared all values in column {column.name}"}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="delete-rows")
    @transaction.atomic
    def delete_rows(self, request, pk=None):
        column = self.get_object()
        if not has_table_access(request.user, column.table, "EDIT"):
            return Response({"error": "No edit access to this table"}, status=status.HTTP_403_FORBIDDEN)
        if column.is_system_column:
            return Response({"error": "Cannot delete rows using system column filter"}, status=status.HTTP_400_BAD_REQUEST)
        
        from django.db.models import Q
        rows = Row.objects.filter(
            table=column.table,
            cells__column=column
        ).exclude(
            Q(cells__value__isnull=True) | Q(cells__value="")
        )
        count = rows.count()
        rows.delete()
        return Response({"message": f"Successfully deleted {count} rows containing values in column {column.name}"}, status=status.HTTP_200_OK)


    def perform_create(self, serializer):
        table = serializer.validated_data["table"]
        from django.db.models import Max
        max_pos = Column.objects.filter(table=table).aggregate(Max("position"))["position__max"] or 0
        serializer.save(position=max_pos + 1)

    @action(detail=False, methods=["post"], url_path="reorder")
    @transaction.atomic
    def reorder_columns(self, request):
        column_ids = request.data.get("columns", [])
        if not column_ids:
            return Response({"error": "No columns list provided"}, status=status.HTTP_400_BAD_REQUEST)
        
        columns = Column.objects.filter(id__in=column_ids)
        if not columns.exists():
            return Response({"error": "No columns found for provided IDs"}, status=status.HTTP_404_NOT_FOUND)
        
        table = columns.first().table
        if columns.filter(table=table).count() != len(column_ids):
            return Response({"error": "All columns must belong to the same table"}, status=status.HTTP_400_BAD_REQUEST)
            
        if not has_table_access(request.user, table, "ADMIN"):
            return Response({"error": "Only admins can reorder columns"}, status=status.HTTP_403_FORBIDDEN)
            
        for index, col_id in enumerate(column_ids):
            Column.objects.filter(id=col_id, table=table).update(position=index + 1)
            
        return Response({"status": "reordered"}, status=status.HTTP_200_OK)

from rest_framework.pagination import PageNumberPagination
from django.db.models import Count

class RowPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500

    def get_paginated_response(self, data):
        table_id = self.request.query_params.get("table")
        if not table_id:
            return super().get_paginated_response(data)
            
        table = get_object_or_404(Table, id=table_id)
        include_stats = self.request.query_params.get("include_stats") == "true"
        
        unique_pids = []
        unique_column_values = {}
        unique_years = []
        status_counts = {}
        priority_counts = {'Urgent': 0, 'High': 0, 'Med': 0, 'Low': 0}
        project_counts = {}
        due_today_count = 0
        overdue_count = 0
        total_qty = 0.0
        completion_stats = {'completed': 0, 'total': 0, 'percent': 0}
        week_actuals = {'calls': 0, 'visits': 0, 'enquiries': 0, 'quotes': 0, 'orders': 0, 'achievementPercent': 0.0}

        if include_stats:
            from django.core.cache import cache
            from django.utils import timezone
            today_str = timezone.localdate().isoformat()
            cache_key = f"table_stats_{table.id}_{today_str}"
            
            cached_data = cache.get(cache_key)
            if cached_data:
                unique_pids = cached_data.get('unique_pids', [])
                unique_column_values = cached_data.get('unique_column_values', {})
                unique_years = cached_data.get('unique_years', [])
                status_counts = cached_data.get('status_counts', {})
                priority_counts = cached_data.get('priority_counts', {'Urgent': 0, 'High': 0, 'Med': 0, 'Low': 0})
                project_counts = cached_data.get('project_counts', {})
                due_today_count = cached_data.get('due_today_count', 0)
                overdue_count = cached_data.get('overdue_count', 0)
                total_qty = cached_data.get('total_qty', 0.0)
                completion_stats = cached_data.get('completion_stats', {'completed': 0, 'total': 0, 'percent': 0})
                week_actuals = cached_data.get('week_actuals', {'calls': 0, 'visits': 0, 'enquiries': 0, 'quotes': 0, 'orders': 0, 'achievementPercent': 0.0})
            else:
                # Calculate statistics
                unique_pids = list(CellValue.objects.filter(
                    column__table=table,
                    column__name='PID',
                    row__is_archived=False
                ).exclude(value=None).values_list('value', flat=True).distinct().order_by('value'))

            # Unique Column values for all filterable columns
            filterable_cols = table.columns.filter(is_filterable=True)
            for col in filterable_cols:
                if col.data_type == 'DROPDOWN':
                    opts = [o.strip() for o in (col.options or '').split(',') if o.strip()]
                    unique_column_values[col.id] = opts
                else:
                    unique_vals = list(CellValue.objects.filter(
                        column=col,
                        row__table=table,
                        row__is_archived=False
                    ).exclude(
                        value__isnull=True
                    ).exclude(
                        value=""
                    ).values_list('value', flat=True).distinct().order_by('value'))
                    cleaned_vals = sorted(list(set(str(v).strip() for v in unique_vals if str(v).strip())))
                    unique_column_values[col.id] = cleaned_vals
            
            # Unique Years
            from django.db.models.functions import ExtractYear
            from tasks.models import Task
            years_qs = Task.objects.filter(
                row__table=table,
                row__is_archived=False
            ).annotate(year=ExtractYear('due_date')).values_list('year', flat=True).distinct().order_by('-year')
            unique_years = [str(y) for y in years_qs if y]
            
            # Status counts
            s_counts = Task.objects.filter(
                row__table=table,
                row__is_archived=False
            ).values('status').annotate(count=Count('id'))
            for item in s_counts:
                val = item['status'] or 'PENDING'
                status_counts[val] = item['count']
                
            # Priority counts
            p_counts = Task.objects.filter(
                row__table=table,
                row__is_archived=False
            ).values('priority').annotate(count=Count('id'))
            for item in p_counts:
                priority = item['priority']
                pl = str(priority).lower()
                if pl.startswith('med'):
                    priority_counts['Med'] += item['count']
                elif pl.startswith('urg'):
                    priority_counts['Urgent'] += item['count']
                elif pl.startswith('hi'):
                    priority_counts['High'] += item['count']
                elif pl.startswith('lo'):
                    priority_counts['Low'] += item['count']
                    
            # Project counts (for List PID)
            pr_counts = CellValue.objects.filter(
                column__table=table,
                column__name='PROJECT',
                row__is_archived=False
            ).values('value').annotate(count=Count('id'))
            for item in pr_counts:
                val = item['value'] or 'No Project'
                project_counts[val] = item['count']
                
            # Tasks due today count
            from django.utils import timezone
            due_today_count = Task.objects.filter(
                row__table=table,
                row__is_archived=False,
                due_date=timezone.localdate()
            ).count()
            
            # Overdue tasks count
            overdue_count = Task.objects.filter(
                row__table=table,
                row__is_archived=False,
                due_date__lt=timezone.localdate()
            ).exclude(status__in=['COMPLETED', 'APPROVED', 'COMPLETE']).count()
            
            # Total QTY (computed in Python to prevent database-specific JSONB casting crashes in PostgreSQL)
            qty_cells = CellValue.objects.filter(
                column__table=table,
                column__name='QTY',
                row__is_archived=False
            ).exclude(value=None).values_list('value', flat=True)
            
            total_qty = 0.0
            for val in qty_cells:
                try:
                    if val is not None and str(val).strip():
                        total_qty += float(str(val).strip())
                except ValueError:
                    pass
                    
            # Completion stats
            total_tasks = Task.objects.filter(row__table=table, row__is_archived=False).count()
            completed_tasks = Task.objects.filter(row__table=table, row__is_archived=False, status__in=['COMPLETED', 'COMPLETE']).count()
            completion_percent = round((completed_tasks / total_tasks) * 100) if total_tasks > 0 else 0
            completion_stats = {
                'completed': completed_tasks,
                'total': total_tasks,
                'percent': completion_percent
            }
            
            # Week actuals for SALES followups
            import datetime
            today_date = timezone.localdate()
            monday = today_date - datetime.timedelta(days=today_date.weekday())
            sunday = monday + datetime.timedelta(days=6)
            
            # Filter row IDs first to avoid loading all cell values
            date_cols = table.columns.filter(name__in=['FOLLOW - UP DATE', 'FOLLOW-UP DATE', 'DATE'])
            row_ids_in_week = list(CellValue.objects.filter(
                column__in=date_cols,
                value__range=[monday.isoformat(), sunday.isoformat()]
            ).values_list('row_id', flat=True).distinct())
            
            calls = 0
            visits = 0
            enquiries = 0
            quotes = 0
            orders = 0

            if row_ids_in_week:
                cells_qs = CellValue.objects.filter(
                    row_id__in=row_ids_in_week,
                    row__is_archived=False,
                    column__name__in=['FOLLOW - UP DATE', 'FOLLOW-UP DATE', 'DATE', 'ACTIVITY TYPE', 'ACTIVITY_TYPE', 'STATUS']
                ).select_related('column')
                
                from collections import defaultdict
                row_cells = defaultdict(dict)
                for cell in cells_qs:
                    row_cells[cell.row_id][cell.column.name] = cell.value

                for r_id, c_dict in row_cells.items():
                    date_val = c_dict.get('FOLLOW - UP DATE') or c_dict.get('FOLLOW-UP DATE') or c_dict.get('DATE')
                    if not date_val:
                        continue
                    try:
                        if isinstance(date_val, str):
                            d = datetime.datetime.strptime(date_val.split('T')[0], "%Y-%m-%d").date()
                        else:
                            continue
                    except Exception:
                        continue

                    if monday <= d <= sunday:
                        act_type = str(c_dict.get('ACTIVITY TYPE') or c_dict.get('ACTIVITY_TYPE') or '').lower().strip()
                        status = str(c_dict.get('STATUS') or '').lower().strip()

                        if 'call' in act_type or 'whatsapp' in act_type or 'linkedin' in act_type:
                            calls += 1
                        if 'site visit' in act_type or 'customer visit' in act_type or act_type == 'visit':
                            visits += 1
                        if 'enquiry' in status or 'enquiries' in status:
                            enquiries += 1
                        if 'quotation' in status or 'quote' in status:
                            quotes += 1
                        if 'order received' in status or 'order' in status:
                            orders += 1

            target_calls = 20
            target_visits = 10
            target_enquiries = 10
            target_orders = 2

            calls_ach = min(100.0, (calls / target_calls) * 100 if target_calls else 0)
            visits_ach = min(100.0, (visits / target_visits) * 100 if target_visits else 0)
            enquiries_ach = min(100.0, (enquiries / target_enquiries) * 100 if target_enquiries else 0)
            orders_ach = min(100.0, (orders / target_orders) * 100 if target_orders else 0)

            achievement_percent = round((calls_ach + visits_ach + enquiries_ach + orders_ach) / 4.0, 2)
            
            week_actuals = {
                'calls': calls,
                'visits': visits,
                'enquiries': enquiries,
                'quotes': quotes,
                'orders': orders,
                'achievementPercent': achievement_percent
            }
            
            cache_data = {
                'unique_pids': unique_pids,
                'unique_column_values': unique_column_values,
                'unique_years': unique_years,
                'status_counts': status_counts,
                'priority_counts': priority_counts,
                'project_counts': project_counts,
                'due_today_count': due_today_count,
                'overdue_count': overdue_count,
                'total_qty': total_qty,
                'completion_stats': completion_stats,
                'week_actuals': week_actuals,
            }
            cache.set(cache_key, cache_data, 86400)
            
        return Response({
            'count': self.page.paginator.count,
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'results': data,
            'unique_pids': unique_pids,
            'unique_years': unique_years,
            'unique_column_values': unique_column_values,
            'stats': {
                'status_counts': status_counts,
                'priority_counts': priority_counts,
                'project_counts': project_counts,
                'due_today_count': due_today_count,
                'overdue_count': overdue_count,
                'total_qty': total_qty,
                'completion_stats': completion_stats,
                'week_actuals': week_actuals
            }
        })

class RowViewSet(viewsets.ModelViewSet):
    serializer_class = RowSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = RowPagination

    def get_queryset(self):
        table_id = self.request.query_params.get("table")
        if not table_id:
            if self.action in ["retrieve", "update", "partial_update", "destroy"]:
                from .permissions import get_accessible_tables
                from django.db.models import Prefetch
                accessible_tables = get_accessible_tables(self.request.user)
                return Row.objects.filter(
                    table__in=accessible_tables, is_archived=False
                ).select_related(
                    'created_by', 'task', 'task__assigned_by'
                ).prefetch_related(
                    Prefetch('cells', queryset=CellValue.objects.select_related('column', 'updated_by')),
                    'task__assigned_to'
                )
            return Row.objects.none()
            
        table = get_object_or_404(Table, id=table_id)
        if not has_table_access(self.request.user, table, "VIEW"):
            return Row.objects.none()
            
        from django.db.models import Prefetch
        queryset = Row.objects.filter(
            table=table, is_archived=False
        ).select_related(
            'created_by', 'task', 'task__assigned_by'
        ).prefetch_related(
            Prefetch('cells', queryset=CellValue.objects.select_related('column', 'updated_by')),
            'task__assigned_to'
        )
        
        # Apply Query Params Filters
        task_id = self.request.query_params.get("task_id")
        if task_id:
            queryset = queryset.filter(task__id=task_id)
            
        # General Row Search Filter (irrespective of case-sensitivity and inline spaces)
        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q, Value, TextField
            from django.db.models.functions import Cast, Lower, Replace
            
            clean_search = search.lower().replace(" ", "")
            
            matching_rows = CellValue.objects.filter(
                row__table=table,
                row__is_archived=False
            ).annotate(
                text_val=Cast('value', TextField())
            ).annotate(
                clean_val=Lower(Replace('text_val', Value(' '), Value(''), output_field=TextField()))
            ).filter(
                clean_val__contains=clean_search
            ).values_list('row_id', flat=True)
            
            queryset = queryset.annotate(
                clean_task_status=Lower(Replace('task__status', Value(' '), Value(''), output_field=TextField())),
                clean_task_priority=Lower(Replace('task__priority', Value(' '), Value(''), output_field=TextField()))
            ).filter(
                Q(id__in=matching_rows) |
                Q(clean_task_status__contains=clean_search) |
                Q(clean_task_priority__contains=clean_search)
            )

        # Custom dynamic column filters
        for key, val in self.request.query_params.items():
            if key.startswith("col_") and val:
                try:
                    col_id = int(key.replace("col_", ""))
                    queryset = queryset.filter(cells__column_id=col_id, cells__value=val)
                except ValueError:
                    pass
        pid = self.request.query_params.get("pid")
        if pid:
            queryset = queryset.filter(cells__column__name='PID', cells__value=pid)
            
        year = self.request.query_params.get("year")
        if year:
            queryset = queryset.filter(task__due_date__year=year)
            
        month = self.request.query_params.get("month")
        if month:
            queryset = queryset.filter(task__due_date__month=month)
            
        due = self.request.query_params.get("due")
        if due:
            import datetime
            from django.utils import timezone
            today = timezone.localdate()
            if due == "today":
                queryset = queryset.filter(task__due_date=today)
            elif due == "this_week":
                monday = today - datetime.timedelta(days=today.weekday())
                sunday = monday + datetime.timedelta(days=6)
                queryset = queryset.filter(task__due_date__range=[monday, sunday])
                
        # Apply Sorting
        sort_by = self.request.query_params.get("sort_by")
        sort_dir = self.request.query_params.get("sort_dir", "asc")
        if sort_by:
            if sort_by.lower() == 'date':
                sort_by = 'date_assigned'
            elif sort_by.lower() in ['enquiry_no', 'enquiry_number', 'enquiry_no/quotation_no']:
                sort_by = 'enquiry_no'
            
            if sort_by == 'enquiry_no':
                from django.db.models import Subquery, OuterRef, TextField, Value
                from django.db.models.functions import Cast, Replace
                enquiry_col = Column.objects.filter(table=table, name__iexact="ENQUIRY_NO/QUOTATION_NO").first()
                if not enquiry_col:
                    enquiry_col = Column.objects.filter(table=table, name__icontains="ENQUIRY").first()
                if enquiry_col:
                    cell_subquery = Subquery(
                        CellValue.objects.filter(row=OuterRef('pk'), column=enquiry_col).values('value')[:1]
                    )
                    queryset = queryset.annotate(
                        enquiry_val=Cast(
                            Replace(
                                Cast(cell_subquery, output_field=TextField()),
                                Value('"'),
                                Value(''),
                                output_field=TextField()
                            ),
                            output_field=TextField()
                        )
                    )
                    if sort_dir == 'desc':
                        queryset = queryset.order_by('-enquiry_val', '-id')
                    else:
                        queryset = queryset.order_by('enquiry_val', 'id')
                else:
                    queryset = queryset.order_by('id')
            elif sort_by == 'due_date' and table.job_type in ['GENERAL', 'ENGINEER', 'LIST_PID']:
                if table.job_type == 'LIST_PID':
                    from django.db.models import Subquery, OuterRef, DateField, TextField, Value
                    from django.db.models.functions import Coalesce, Cast, Replace
                    due_col = Column.objects.filter(table=table, name__iexact="DUE_DATE_FLOW_FORCE").first()
                    if due_col:
                        cell_subquery = Subquery(
                            CellValue.objects.filter(row=OuterRef('pk'), column=due_col).values('value')[:1]
                        )
                        queryset = queryset.annotate(
                            due_date_val=Cast(
                                Replace(
                                    Cast(cell_subquery, output_field=TextField()),
                                    Value('"'),
                                    Value(''),
                                    output_field=TextField()
                                ),
                                output_field=DateField()
                            )
                        )
                        if sort_dir == 'desc':
                            queryset = queryset.order_by('-due_date_val', '-id')
                        else:
                            queryset = queryset.order_by('due_date_val', 'id')
                    else:
                        if sort_dir == 'desc':
                            queryset = queryset.order_by('-task__due_date', '-id')
                        else:
                            queryset = queryset.order_by('task__due_date', 'id')
                else:
                    if sort_dir == 'desc':
                        queryset = queryset.order_by('-task__due_date', '-id')
                    else:
                        queryset = queryset.order_by('task__due_date', 'id')
            elif sort_by == 'follow_up_date' and table.job_type == 'SALES':
                from django.db.models import Max, DateField
                from django.db.models.functions import Coalesce
                queryset = queryset.annotate(
                    latest_follow_up=Max('task__follow_ups__follow_up_date')
                ).annotate(
                    sorted_follow_up=Coalesce('latest_follow_up', 'task__due_date', output_field=DateField())
                )
                if sort_dir == 'desc':
                    queryset = queryset.order_by('-sorted_follow_up', '-id')
                else:
                    queryset = queryset.order_by('sorted_follow_up', 'id')
            elif sort_by == 'date_assigned':
                from django.db.models import Subquery, OuterRef, DateField, TextField, Value
                from django.db.models.functions import Coalesce, Cast, Replace
                date_col = Column.objects.filter(table=table, name__iexact="DATE").first()
                if date_col:
                    cell_subquery = Subquery(
                        CellValue.objects.filter(row=OuterRef('pk'), column=date_col).values('value')[:1]
                    )
                    queryset = queryset.annotate(
                        date_assigned_val=Cast(
                            Replace(
                                Cast(cell_subquery, output_field=TextField()),
                                Value('"'),
                                Value(''),
                                output_field=TextField()
                            ),
                            output_field=DateField()
                        )
                    ).annotate(
                        sorted_date_assigned=Coalesce('date_assigned_val', Cast('created_at', output_field=DateField()), output_field=DateField())
                    )
                else:
                    queryset = queryset.annotate(
                        sorted_date_assigned=Cast('created_at', output_field=DateField())
                    )
                if sort_dir == 'desc':
                    queryset = queryset.order_by('-sorted_date_assigned', '-id')
                else:
                    queryset = queryset.order_by('sorted_date_assigned', 'id')

        return queryset

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance.table, "EDIT"):
            return Response({"error": "No edit access to this table"}, status=status.HTTP_403_FORBIDDEN)
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        table_id = request.data.get("table")
        table = get_object_or_404(Table, id=table_id)
        
        if not has_table_access(request.user, table, "EDIT"):
            return Response({"error": "No edit access to this table"}, status=status.HTTP_403_FORBIDDEN)

        cells_data = request.data.get("cells", {})
        
        is_sales = table.job_type == "SALES"
        is_list_pid = table.job_type == "LIST_PID"
        is_personal = table.job_type == "PERSONAL"
        
        # Verify DUE_DATE/FOLLOW_UP_DATE and TASK_NAME/CUSTOMER_NAME are present
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
        else:
            due_date_str = cells_data.get("DUE_DATE")
            task_name = cells_data.get("TASK_NAME")
            date_field_name = "DUE_DATE"
            name_field_name = "TASK_NAME"

        priority = cells_data.get("priority", "MEDIUM")

        due_date = None
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str.split("T")[0], "%Y-%m-%d").date()
            except ValueError:
                return Response({"error": f"Invalid {date_field_name} format. Use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        elif not is_list_pid and not is_personal:
            return Response({"error": f"{date_field_name} is mandatory"}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Create Row
        row = Row.objects.create(table=table, created_by=request.user)

        # Get system columns
        cols = {col.name: col for col in table.columns.all()}

        # 2. Compute S_NO
        latest_s_no = 0
        s_no_col = cols.get("S_NO")
        if s_no_col:
            latest_cell = CellValue.objects.filter(column=s_no_col).order_by("-id").first()
            if latest_cell and isinstance(latest_cell.value, int):
                latest_s_no = latest_cell.value
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
                CellValue.objects.create(row=row, column=col, value=val, updated_by=request.user)

        # 3. Create Task
        task = Task.objects.create(
            row=row,
            due_date=due_date,
            priority=priority,
            status="PENDING",
            assigned_by=request.user
        )

        # Look for any cell value belonging to a USER column or assignee column to set assignee
        user_to_assign = None
        from django.db.models import Q
        for col_name, val in cell_values.items():
            col = cols.get(col_name)
            if col and (col.data_type == "USER" or col_name.upper() in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]):
                if val:
                    # Resolve user
                    try:
                        if str(val).isdigit():
                            user_to_assign = EmployeeUser.objects.get(id=int(val), is_active=True)
                        elif "@" in str(val):
                            user_to_assign = EmployeeUser.objects.get(email=val, is_active=True)
                        else:
                            user_to_assign = EmployeeUser.objects.get(full_name__iexact=val, is_active=True)
                    except EmployeeUser.DoesNotExist:
                        user_to_assign = EmployeeUser.objects.filter(
                            Q(full_name__icontains=val) | Q(email__icontains=val),
                            is_active=True
                        ).first()
                    break

        # Handle assignments if provided
        assigned_to_ids = request.data.get("assigned_to", [])
        if assigned_to_ids:
            employees = EmployeeUser.objects.filter(id__in=assigned_to_ids)
            task.assigned_to.set(employees)
        elif user_to_assign:
            task.assigned_to.set([user_to_assign])
        
        # Log creation
        ActivityLog.objects.create(
            task=task,
            action="Created Task Row",
            user=request.user,
            details={"task_name": task_name, "due_date": due_date_str}
        )

        return Response(RowSerializer(row).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="edit-cell")
    @transaction.atomic
    def edit_cell(self, request, pk=None):
        row = get_object_or_404(Row, pk=pk)
        table = row.table

        task = getattr(row, "task", None)
        is_assigned = False
        if task:
            is_assigned = task.assigned_to.filter(id=request.user.id).exists()

        if not (has_table_access(request.user, table, "EDIT") or is_assigned):
            return Response({"error": "No edit access to this table or task row"}, status=status.HTTP_403_FORBIDDEN)

        column_id = request.data.get("column")
        value = request.data.get("value")

        column = get_object_or_404(Column, id=column_id, table=table)

        # Enforce column level permissions
        if table.job_type != "LIST_PID":
            if is_assigned:
                if column.name == "S_NO" or column.name in ["INITIAL_MAIL", "ALERT_MAIL"]:
                    return Response({"error": f"Column {column.name} is read-only for assignees"}, status=status.HTTP_403_FORBIDDEN)
            else:
                perm = get_column_access_level(request.user, column)
                if perm != "EDITABLE":
                    return Response({"error": f"Column {column.name} is read-only or hidden for you"}, status=status.HTTP_403_FORBIDDEN)

        # Update CellValue
        cell, created = CellValue.objects.update_or_create(
            row=row, column=column,
            defaults={"value": value, "updated_by": request.user}
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
                            new_date = datetime.strptime(str(ff_val.value).split("T")[0], "%Y-%m-%d").date()
                        except ValueError:
                            pass
                    if not new_date and cust_val and cust_val.value:
                        try:
                            new_date = datetime.strptime(str(cust_val.value).split("T")[0], "%Y-%m-%d").date()
                        except ValueError:
                            pass
                else:
                    if column.name in ["DUE_DATE", "FOLLOW_UP_DATE", "DUE_DATE_FLOW_FORCE", "DUE_DATE_CUSTOMER"]:
                        try:
                            new_date = datetime.strptime(str(value).split("T")[0], "%Y-%m-%d").date()
                        except ValueError:
                            return Response({"error": "Invalid date format"}, status=status.HTTP_400_BAD_REQUEST)

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
                elif not is_list_pid:
                    return Response({"error": "Invalid date format"}, status=status.HTTP_400_BAD_REQUEST)
            elif not is_list_pid:
                # No task but system column
                pass
        elif column.name in ["TASK_NAME", "CUSTOMER_NAME", "ENQUIRY_NO"] and column.is_system_column:
                    # Activity log detail update
                    pass

        # Sync with Task assigned_to if column data_type is USER or column name represents assignment
        col_name_upper = column.name.upper()
        if col_name_upper == "STATUS":
            task = getattr(row, "task", None)
            if task:
                val_upper = str(value).upper().strip().replace(" ", "_")
                if val_upper in ["COMPLETE", "COMPLETED"]:
                    val_upper = "COMPLETED"
                valid_statuses = [choice[0] for choice in Task.STATUS_CHOICES]
                if val_upper in valid_statuses:
                    task.status = val_upper
                    task.save(update_fields=["status"])

        if column.data_type == "USER" or col_name_upper in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]:
            task = getattr(row, "task", None)
            if task:
                from django.db.models import Q
                try:
                    if value:
                        # Try parsing as ID first
                        if str(value).isdigit():
                            user = EmployeeUser.objects.get(id=int(value), is_active=True)
                        elif "@" in str(value):
                            user = EmployeeUser.objects.get(email=value, is_active=True)
                        else:
                            user = EmployeeUser.objects.get(full_name__iexact=value, is_active=True)
                        
                        task.assigned_to.set([user])
                        task.assigned_by = request.user
                        task.save()
                    else:
                        task.assigned_to.clear()
                except EmployeeUser.DoesNotExist:
                    # Fallback to case-insensitive partial match on full_name/email
                    if value:
                        user = EmployeeUser.objects.filter(
                            Q(full_name__icontains=value) | Q(email__icontains=value),
                            is_active=True
                        ).first()
                        if user:
                            task.assigned_to.set([user])
                            task.assigned_by = request.user
                            task.save()
                        else:
                            task.assigned_to.clear()

        # Log change
        task = getattr(row, "task", None)
        if task:
            ActivityLog.objects.create(
                task=task,
                action=f"Updated cell {column.name}",
                user=request.user,
                details={"column": column.name, "value": value}
            )

        return Response(RowSerializer(row).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="edit-row")
    @transaction.atomic
    def edit_row(self, request, pk=None):
        row = get_object_or_404(Row, pk=pk)
        table = row.table

        task = getattr(row, "task", None)
        is_assigned = False
        if task:
            is_assigned = task.assigned_to.filter(id=request.user.id).exists()

        if not (has_table_access(request.user, table, "EDIT") or is_assigned):
            return Response({"error": "No edit access to this table or task row"}, status=status.HTTP_403_FORBIDDEN)

        cells_data = request.data.get("cells", {})
        cols = {col.name: col for col in table.columns.all()}

        updated_columns = []
        for col_name, value in cells_data.items():
            column = cols.get(col_name)
            if not column:
                continue

            # Enforce column level permissions
            if table.job_type != "LIST_PID":
                if is_assigned:
                    if column.name == "S_NO" or column.name in ["INITIAL_MAIL", "ALERT_MAIL"]:
                        continue
                else:
                    perm = get_column_access_level(request.user, column)
                    if perm != "EDITABLE":
                        continue

            # Update CellValue
            CellValue.objects.update_or_create(
                row=row, column=column,
                defaults={"value": value, "updated_by": request.user}
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
                                new_date = datetime.strptime(str(ff_val.value).split("T")[0], "%Y-%m-%d").date()
                            except ValueError:
                                pass
                        if not new_date and cust_val and cust_val.value:
                            try:
                                new_date = datetime.strptime(str(cust_val.value).split("T")[0], "%Y-%m-%d").date()
                            except ValueError:
                                pass
                    else:
                        if column.name in ["DUE_DATE", "FOLLOW_UP_DATE", "DUE_DATE_FLOW_FORCE", "DUE_DATE_CUSTOMER"]:
                            try:
                                new_date = datetime.strptime(str(value).split("T")[0], "%Y-%m-%d").date()
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

            # Sync with Task assigned_to if column data_type is USER or column name represents assignment
            col_name_upper = column.name.upper()
            if col_name_upper == "STATUS":
                task = getattr(row, "task", None)
                if task:
                    val_upper = str(value).upper().strip().replace(" ", "_")
                    if val_upper in ["COMPLETE", "COMPLETED"]:
                        val_upper = "COMPLETED"
                    valid_statuses = [choice[0] for choice in Task.STATUS_CHOICES]
                    if val_upper in valid_statuses:
                        task.status = val_upper
                        task.save(update_fields=["status"])

            if column.data_type == "USER" or col_name_upper in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]:
                task = getattr(row, "task", None)
                if task:
                    from django.db.models import Q
                    try:
                        if value:
                            if str(value).isdigit():
                                user = EmployeeUser.objects.get(id=int(value), is_active=True)
                            elif "@" in str(value):
                                user = EmployeeUser.objects.get(email=value, is_active=True)
                            else:
                                user = EmployeeUser.objects.get(full_name__iexact=value, is_active=True)
                            
                            task.assigned_to.set([user])
                            task.assigned_by = request.user
                            task.save()
                        else:
                            task.assigned_to.clear()
                    except EmployeeUser.DoesNotExist:
                        if value:
                            user = EmployeeUser.objects.filter(
                                Q(full_name__icontains=value) | Q(email__icontains=value),
                                is_active=True
                            ).first()
                            if user:
                                task.assigned_to.set([user])
                                task.assigned_by = request.user
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
                user=request.user,
                details={"updated_columns": updated_columns}
            )

        return Response(RowSerializer(row).data, status=status.HTTP_200_OK)

class TableAccessViewSet(viewsets.ModelViewSet):
    serializer_class = TableAccessSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return TableAccess.objects.all().select_related('user', 'department')

class ColumnAccessViewSet(viewsets.ModelViewSet):
    serializer_class = ColumnAccessSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ColumnAccess.objects.all().select_related('user')

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required

@login_required
def table_spreadsheet_view(request, table_id):
    table = get_object_or_404(Table.objects.prefetch_related('columns'), id=table_id)
    if not has_table_access(request.user, table, "VIEW"):
        return redirect("/")
    has_edit = has_table_access(request.user, table, "EDIT")
    has_admin = has_table_access(request.user, table, "ADMIN")
    return render(request, "tables/table_spreadsheet.html", {
        "table": table,
        "has_edit_access": has_edit,
        "has_admin_access": has_admin
    })

@login_required
def table_create_view(request):
    if request.user.role not in ["SUPER_ADMIN", "ADMIN"]:
        return redirect("/")
    if request.method == "POST":
        name = request.POST.get("name")
        description = request.POST.get("description")
        job_type = request.POST.get("job_type", "GENERAL")
        table = Table.objects.create(name=name, description=description, job_type=job_type, created_by=request.user)
        # Create TableAccess for the creator as ADMIN
        TableAccess.objects.create(table=table, user=request.user, access_level="ADMIN")
        return redirect(f"/tables/{table.id}/")
    return render(request, "tables/table_create.html")

@login_required
def table_list_view(request):
    from django.contrib import messages
    from auth_app.models import EmployeeUser
    from .models import Table, TableAccess
    from .permissions import get_accessible_tables

    is_admin = request.user.role in ["SUPER_ADMIN", "ADMIN"]

    if request.method == "POST" and is_admin:
        action = request.POST.get("action")

        if action == "create":
            name = request.POST.get("name")
            description = request.POST.get("description")
            job_type = request.POST.get("job_type", "GENERAL")
            if name:
                table = Table.objects.create(name=name, description=description, job_type=job_type, created_by=request.user)
                TableAccess.objects.create(
                    table=table, user=request.user, access_level="ADMIN"
                )
                messages.success(request, f"Table '{name}' created successfully.")
            return redirect("tables:table_list")

        elif action == "delete":
            table_id = request.POST.get("table_id")
            if not table_id or not str(table_id).isdigit():
                messages.error(request, "Invalid table selected.")
                return redirect("tables:table_list")
            table = get_object_or_404(Table, id=table_id)
            if table.created_by == request.user or request.user.role == "SUPER_ADMIN":
                table.delete()
                messages.success(request, "Table deleted successfully.")
            else:
                messages.error(request, "You do not have permission to delete this table.")
            return redirect("tables:table_list")

        elif action == "grant":
            table_id = request.POST.get("table_id")
            user_id = request.POST.get("user_id")
            access_level = request.POST.get("access_level", "EDIT")
            if not table_id or not str(table_id).isdigit() or not user_id or not str(user_id).isdigit():
                messages.error(request, "Please select a valid table and employee.")
                return redirect("tables:table_list")
            table = get_object_or_404(Table, id=table_id)
            user = get_object_or_404(EmployeeUser, id=user_id)

            TableAccess.objects.update_or_create(
                table=table, user=user,
                defaults={"access_level": access_level}
            )
            messages.success(request, f"Access granted to {user.full_name or user.email}.")
            return redirect("tables:table_list")

        elif action == "revoke":
            table_id = request.POST.get("table_id")
            user_id = request.POST.get("user_id")
            if not table_id or not str(table_id).isdigit() or not user_id or not str(user_id).isdigit():
                messages.error(request, "Please select a valid table and employee.")
                return redirect("tables:table_list")
            table = get_object_or_404(Table, id=table_id)
            user = get_object_or_404(EmployeeUser, id=user_id)

            TableAccess.objects.filter(table=table, user=user).delete()
            messages.success(request, f"Access revoked for {user.full_name or user.email}.")
            return redirect("tables:table_list")

        elif action == "change_access":
            table_id = request.POST.get("table_id")
            user_id = request.POST.get("user_id")
            access_level = request.POST.get("access_level")
            if not table_id or not str(table_id).isdigit() or not user_id or not str(user_id).isdigit():
                messages.error(request, "Please select a valid table and employee.")
                return redirect("tables:table_list")
            table = get_object_or_404(Table, id=table_id)
            user = get_object_or_404(EmployeeUser, id=user_id)

            TableAccess.objects.filter(table=table, user=user).update(access_level=access_level)
            messages.success(request, f"Access level updated to {access_level}.")
            return redirect("tables:table_list")

    # GET handling
    if is_admin:
        tables = Table.objects.filter(is_active=True).prefetch_related('access_rules__user')
        employees = EmployeeUser.objects.filter(is_active=True).exclude(role="SUPER_ADMIN")
    else:
        tables = get_accessible_tables(request.user)
        employees = None

    return render(
        request,
        "tables/table_list.html",
        {
            "tables": tables,
            "employees": employees,
            "is_admin": is_admin,
        }
    )

@login_required
def tables_analytics_dashboard(request):
    from tables.models import Table
    from tables.permissions import get_accessible_tables
    from tasks.models import Task
    from django.utils import timezone

    if request.user.role in ["SUPER_ADMIN", "ADMIN"]:
        tables = Table.objects.filter(is_active=True)
    else:
        tables = get_accessible_tables(request.user)

    from django.db.models import Count, Q
    today = timezone.localdate()

    annotated_tables = tables.annotate(
        total_tasks=Count('rows__task', filter=Q(rows__is_archived=False)),
        pending_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__status="PENDING")),
        in_progress_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__status="IN_PROGRESS")),
        ready_for_review_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__status="READY_FOR_REVIEW")),
        completed_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__status__in=["COMPLETED", "APPROVED"])),
        overdue_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__due_date__lt=today) & ~Q(rows__task__status__in=["COMPLETED", "APPROVED"])),
        due_today_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__due_date=today)),
        low_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__priority="LOW")),
        medium_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__priority="MEDIUM")),
        high_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__priority="HIGH")),
        critical_tasks=Count('rows__task', filter=Q(rows__is_archived=False, rows__task__priority="CRITICAL"))
    )

    tables_data = []
    for table in annotated_tables:
        total = table.total_tasks
        completed = table.completed_tasks
        completion_rate = int(completed * 100 / total) if total > 0 else 0

        tables_data.append({
            "table": table,
            "total": total,
            "pending": table.pending_tasks,
            "in_progress": table.in_progress_tasks,
            "ready_for_review": table.ready_for_review_tasks,
            "completed": completed,
            "overdue": table.overdue_tasks,
            "due_today": table.due_today_tasks,
            "low": table.low_tasks,
            "medium": table.medium_tasks,
            "high": table.high_tasks,
            "critical": table.critical_tasks,
            "completion_rate": completion_rate,
        })

    return render(
        request,
        "tables/analytics_dashboard.html",
        {
            "tables_data": tables_data,
        },
    )


