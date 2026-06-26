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
        return get_accessible_tables(self.request.user)

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
        if row_ids is None:
            rows = Row.objects.filter(table=table)
        else:
            if not isinstance(row_ids, list):
                return Response({"error": "row_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
            rows = Row.objects.filter(table=table, id__in=row_ids)

        today = timezone.localdate()
        # Find all associated tasks that are overdue (due_date < today) and not completed/approved
        tasks = Task.objects.filter(row__in=rows, due_date__lt=today).exclude(status__in=['COMPLETED', 'APPROVED'])

        from django.template.loader import render_to_string
        from tasks.models import EmailLog
        from tasks.tasks import send_email_log_task

        sent_count = 0
        for task in tasks:
            days_overdue = (today - task.due_date).days
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
                    subject = f"ESCALATION: Overdue Task - {task.task_name} ({days_overdue} days overdue)"
                    task_link = f"http://localhost:8000/tables/{task.row.table_id}/?open_task_id={task.id}"

                    context = {
                        'recipient_name': recipient.full_name,
                        'days': days_overdue,
                        'task_name': task.task_name,
                        'due_date': str(task.due_date),
                        'employee_name': ", ".join([u.full_name for u in task.assigned_to.all()]),
                        'department_name': task.row.table.department.name if task.row.table.department else "Global",
                        'status': task.status,
                        'priority': task.priority,
                        'task_link': task_link,
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

    def _import_rows_from_csv_data(self, file_data, table, request_user, header_row=None, data_row=None):
        import csv
        import io
        from django.utils.dateparse import parse_date
        from django.utils import timezone

        lines = file_data.splitlines()
        
        # 1. Dynamically locate the header row
        header_idx = -1
        is_sales = table.job_type == "SALES"
        
        if header_row is not None and data_row is not None:
            try:
                header_line = lines[int(header_row) - 1]
                data_lines = lines[int(data_row) - 1:]
                lines_to_parse = [header_line] + data_lines
            except IndexError:
                return None, f"Specified header row ({header_row}) or data row ({data_row}) is out of bounds."
        else:
            for idx, line in enumerate(lines):
                tokens = [t.strip().upper() for t in line.split(",")]
                # Check if this line looks like our header row
                # It should contain S_NO and task name / customer name and due date / follow up date
                has_s_no = any(t in ["S_NO", "S.NO", "S. NO.", "SL_NO", "SL.NO", "SL. NO.", "S NO", "SL NO"] for t in tokens)
                if is_sales:
                    has_task = any("CUSTOMER" in t or "CLIENT" in t or "TASK" in t for t in tokens)
                    has_due = any("FOLLOW" in t or "UP" in t or "DUE" in t for t in tokens)
                else:
                    has_task = any("TASK" in t for t in tokens)
                    has_due = any("DUE" in t for t in tokens)
                if has_s_no and has_task and has_due:
                    header_idx = idx
                    break

            if header_idx != -1:
                lines_to_parse = lines[header_idx:]
            else:
                if len(lines) >= 12:
                    lines_to_parse = lines[11:]
                else:
                    lines_to_parse = lines

        io_string = io.StringIO("\n".join(lines_to_parse))
        reader = csv.DictReader(io_string)

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
            if h in ["TASK_NAME", "TASKNAME", "TASK", "CUSTOMER_NAME", "CUSTOMERNAME", "CUSTOMER", "CLIENT_NAME", "CLIENTNAME", "CLIENT"]:
                return "CUSTOMER_NAME" if is_sales else "TASK_NAME"
            if h in ["DUE_DATE", "DUEDATE", "FOLLOW_UP_DATE", "FOLLOWUPDATE", "FOLLOW_UP"]:
                return "FOLLOW_UP_DATE" if is_sales else "DUE_DATE"
            if h in ["INITIAL_MAIL", "INITIALMAIL"]:
                return "INITIAL_MAIL"
            if h in ["ALERT_MAIL", "ALERTMAIL"]:
                return "ALERT_MAIL"
            if h in ["S_NO", "SNO", "SL_NO", "SLNO", "SERIAL_NO", "SERIALNO", "S_NO_"]:
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

        # Check for column equality
        missing_in_csv = db_col_names - set(csv_headers)
        extra_in_csv = set(csv_headers) - db_col_names

        # If there's a mismatch, return user-friendly error message
        if missing_in_csv or extra_in_csv:
            err_msg = "Column mismatch. Please ensure all columns are equal."
            if missing_in_csv:
                missing_orig = [normalized_db_cols[norm].name for norm in missing_in_csv]
                err_msg += f" Missing in sheet: {', '.join(sorted(missing_orig))}."
            if extra_in_csv:
                # To display original extra column name
                extra_orig = []
                for name in reader.fieldnames:
                    if header_mapping.get(name) in extra_in_csv:
                        extra_orig.append(name)
                err_msg += f" Extra in sheet: {', '.join(sorted(extra_orig))}."
            return None, err_msg

        created_rows = []
        for row_dict in reader:
            normalized_row = {}
            for original_key, val in row_dict.items():
                if not original_key:
                    continue
                normalized_key = header_mapping.get(original_key)
                if normalized_key:
                    normalized_row[normalized_key] = val

            if is_sales:
                task_name = normalized_row.get("CUSTOMER_NAME")
                due_date_str = normalized_row.get("FOLLOW_UP_DATE")
            else:
                task_name = normalized_row.get("TASK_NAME")
                due_date_str = normalized_row.get("DUE_DATE")
            
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

            if not task_name or not due_date_str:
                continue

            due_date = parse_date(due_date_str)
            if not due_date:
                continue

            # Create Row
            row = Row.objects.create(table=table, created_by=request_user)

            # Auto compute S_NO
            latest_s_no = 0
            s_no_col = normalized_db_cols.get("S_NO")
            if s_no_col:
                latest_cell = CellValue.objects.filter(column=s_no_col).order_by("-id").first()
                if latest_cell and isinstance(latest_cell.value, int):
                    latest_s_no = latest_cell.value
            s_no = latest_s_no + 1

            # Parse and normalize other system fields
            csv_date_str = normalized_row.get("DATE")
            date_val = None
            if csv_date_str:
                parsed_d = parse_date(csv_date_str)
                if parsed_d:
                    date_val = parsed_d.isoformat()
            if not date_val:
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

            system_field_names = ["S_NO", "DATE", "FOLLOW_UP_DATE", "CUSTOMER_NAME", "INITIAL_MAIL", "ALERT_MAIL"] if is_sales else ["S_NO", "DATE", "DUE_DATE", "TASK_NAME", "INITIAL_MAIL", "ALERT_MAIL"]
            if is_sales:
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "FOLLOW_UP_DATE": due_date.isoformat(),
                    "CUSTOMER_NAME": task_name,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }
            else:
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "DUE_DATE": due_date.isoformat(),
                    "TASK_NAME": task_name,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }

            # Map remaining custom columns including status, priority, and date columns
            for col_name, val in normalized_row.items():
                if col_name not in system_field_names:
                    if col_name in db_col_names:
                        col = normalized_db_cols[col_name]
                        cell_values[col.name] = val

            for name, val in cell_values.items():
                col = None
                if name in system_field_names:
                    col = normalized_db_cols.get(name)
                else:
                    col = next((c for c in table.columns.all() if c.name == name), None)
                if col:
                    CellValue.objects.create(row=row, column=col, value=val, updated_by=request_user)

            # Normalize priority and status for Task model
            norm_priority = str(priority).upper().strip().replace(" ", "_")
            if norm_priority not in [choice[0] for choice in Task.PRIORITY_CHOICES]:
                norm_priority = "MEDIUM"

            norm_status = str(status_val).upper().strip().replace(" ", "_")
            if norm_status in ["COMPLETE", "COMPLETED"]:
                norm_status = "COMPLETED"
            elif norm_status not in [choice[0] for choice in Task.STATUS_CHOICES]:
                norm_status = "PENDING"

            # Create Task
            task = Task.objects.create(
                row=row,
                due_date=due_date,
                priority=norm_priority,
                status=norm_status,
                assigned_by=request_user,
                initial_mail_sent=(initial_mail_val == "YES"),
                alert_mail_sent=(alert_mail_val == "YES")
            )

            # Resolve assignee user if provided in any USER data_type column or header representation
            user_to_assign = None
            from django.db.models import Q
            for col_name, val in cell_values.items():
                col = None
                if col_name in system_field_names:
                    col = normalized_db_cols.get(col_name)
                else:
                    col = next((c for c in table.columns.all() if c.name == col_name), None)

                if col and (col.data_type == "USER" or col.name.upper() in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]):
                    if val:
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

            if user_to_assign:
                task.assigned_to.set([user_to_assign])

            created_rows.append(row)

        return created_rows, None

    @action(detail=True, methods=["post"], url_path="import-csv")
    @transaction.atomic
    def import_csv(self, request, pk=None):
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

    @action(detail=True, methods=["post"], url_path="import-google-sheet")
    @transaction.atomic
    def import_google_sheet(self, request, pk=None):
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
            if self.action in ["retrieve", "update", "partial_update", "destroy"]:
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
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance.table, "ADMIN"):
            return Response({"error": "Only admins can update columns"}, status=status.HTTP_403_FORBIDDEN)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if not has_table_access(request.user, instance.table, "ADMIN"):
            return Response({"error": "Only admins can delete columns"}, status=status.HTTP_403_FORBIDDEN)
        if instance.is_system_column:
            return Response({"error": "Cannot delete system columns"}, status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

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

class RowViewSet(viewsets.ModelViewSet):
    serializer_class = RowSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        table_id = self.request.query_params.get("table")
        if not table_id:
            if self.action in ["retrieve", "update", "partial_update", "destroy"]:
                from .permissions import get_accessible_tables
                accessible_tables = get_accessible_tables(self.request.user)
                return Row.objects.filter(table__in=accessible_tables, is_archived=False)
            return Row.objects.none()
        table = get_object_or_404(Table, id=table_id)
        if not has_table_access(self.request.user, table, "VIEW"):
            return Row.objects.none()
        return Row.objects.filter(table=table, is_archived=False)

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
        
        # Verify DUE_DATE/FOLLOW_UP_DATE and TASK_NAME/CUSTOMER_NAME are present
        if is_sales:
            due_date_str = cells_data.get("FOLLOW_UP_DATE")
            task_name = cells_data.get("CUSTOMER_NAME")
            date_field_name = "FOLLOW_UP_DATE"
            name_field_name = "CUSTOMER_NAME"
        else:
            due_date_str = cells_data.get("DUE_DATE")
            task_name = cells_data.get("TASK_NAME")
            date_field_name = "DUE_DATE"
            name_field_name = "TASK_NAME"

        priority = cells_data.get("priority", "MEDIUM")

        if not due_date_str:
            return Response({"error": f"{date_field_name} is mandatory"}, status=status.HTTP_400_BAD_REQUEST)
        if not task_name:
            return Response({"error": f"{name_field_name} is mandatory"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            due_date = datetime.strptime(due_date_str.split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            return Response({"error": f"Invalid {date_field_name} format. Use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)

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
                "FOLLOW_UP_DATE": due_date.isoformat(),
                "CUSTOMER_NAME": task_name,
                "INITIAL_MAIL": "NO",
                "ALERT_MAIL": "NO"
            }
        else:
            cell_values = {
                "S_NO": s_no,
                "DATE": timezone.localdate().isoformat(),
                "DUE_DATE": due_date.isoformat(),
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
        if column.is_system_column:
            task = getattr(row, "task", None)
            if task:
                if column.name in ["DUE_DATE", "FOLLOW_UP_DATE"]:
                    try:
                        task.due_date = datetime.strptime(value.split("T")[0], "%Y-%m-%d").date()
                        task.save(update_fields=["due_date"])
                    except ValueError:
                        return Response({"error": "Invalid date format"}, status=status.HTTP_400_BAD_REQUEST)
                elif column.name == "TASK_NAME":
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
            if column.is_system_column:
                task = getattr(row, "task", None)
                if task:
                    if column.name in ["DUE_DATE", "FOLLOW_UP_DATE"]:
                        try:
                            task.due_date = datetime.strptime(str(value).split("T")[0], "%Y-%m-%d").date()
                            task.save(update_fields=["due_date"])
                        except ValueError:
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
        return TableAccess.objects.all()

class ColumnAccessViewSet(viewsets.ModelViewSet):
    serializer_class = ColumnAccessSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ColumnAccess.objects.all()

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required

@login_required
def table_spreadsheet_view(request, table_id):
    table = get_object_or_404(Table, id=table_id)
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

