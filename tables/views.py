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
            department=table.department
        )

        # Clone custom columns (excluding system columns as they are auto-created in save())
        for col in table.columns.filter(is_system_column=False):
            Column.objects.create(
                table=new_table,
                name=col.name,
                data_type=col.data_type,
                is_mandatory=col.is_mandatory,
                is_system_column=False,
                position=col.position
            )

        # Clone TableAccess
        for access in table.access_rules.all():
            TableAccess.objects.create(
                table=new_table,
                user=access.user,
                department=access.department,
                access_level=access.access_level
            )

        return Response(TableSerializer(new_table).data, status=status.HTTP_201_CREATED)

    def _import_rows_from_csv_data(self, file_data, table, request_user):
        import csv
        import io
        from django.utils.dateparse import parse_date
        from django.utils import timezone

        lines = file_data.splitlines()
        # The header row starts at row 12, data starts at row 13.
        # Skip first 11 lines to get to the header row.
        if len(lines) >= 12:
            lines_to_parse = lines[11:]
        else:
            lines_to_parse = lines

        io_string = io.StringIO("\n".join(lines_to_parse))
        reader = csv.DictReader(io_string)

        if not reader.fieldnames:
            return None, "Import file is empty or invalid"

        db_cols = {col.name: col for col in table.columns.all()}
        db_col_names = set(db_cols.keys())

        # Normalize CSV fieldnames to match DB columns
        csv_headers = []
        for name in reader.fieldnames:
            if not name:
                continue
            normalized = name.strip()
            if normalized == "Task Name":
                normalized = "TASK_NAME"
            elif normalized == "Due Date":
                normalized = "DUE_DATE"
            elif normalized.upper() in db_col_names:
                normalized = normalized.upper()
            csv_headers.append(normalized)

        # Check for column equality
        missing_in_csv = db_col_names - set(csv_headers)
        extra_in_csv = set(csv_headers) - db_col_names

        if missing_in_csv or extra_in_csv:
            err_msg = "Column mismatch. Please ensure all columns are equal."
            if missing_in_csv:
                err_msg += f" Missing in sheet: {', '.join(sorted(missing_in_csv))}."
            if extra_in_csv:
                err_msg += f" Extra in sheet: {', '.join(sorted(extra_in_csv))}."
            return None, err_msg

        created_rows = []
        for row_dict in reader:
            normalized_row = {}
            for original_key, val in row_dict.items():
                if not original_key:
                    continue
                normalized_key = original_key.strip()
                if normalized_key == "Task Name":
                    normalized_key = "TASK_NAME"
                elif normalized_key == "Due Date":
                    normalized_key = "DUE_DATE"
                elif normalized_key.upper() in db_col_names:
                    normalized_key = normalized_key.upper()
                normalized_row[normalized_key] = val

            task_name = normalized_row.get("TASK_NAME")
            due_date_str = normalized_row.get("DUE_DATE")
            priority = normalized_row.get("PRIORITY") or normalized_row.get("Priority") or "MEDIUM"
            status_val = normalized_row.get("STATUS") or normalized_row.get("Status") or "PENDING"

            if not task_name or not due_date_str:
                continue

            due_date = parse_date(due_date_str)
            if not due_date:
                continue

            # Create Row
            row = Row.objects.create(table=table, created_by=request_user)

            # Auto compute S_NO
            latest_s_no = 0
            s_no_col = db_cols.get("S_NO")
            if s_no_col:
                latest_cell = CellValue.objects.filter(column=s_no_col).order_by("-id").first()
                if latest_cell and isinstance(latest_cell.value, int):
                    latest_s_no = latest_cell.value
            s_no = latest_s_no + 1

            cell_values = {
                "S_NO": s_no,
                "DATE": timezone.localdate().isoformat(),
                "DUE_DATE": due_date.isoformat(),
                "TASK_NAME": task_name,
                "INITIAL_MAIL": "NO",
                "ALERT_MAIL": "NO"
            }

            # Map remaining headers to custom columns
            for col_name, val in normalized_row.items():
                if col_name not in ["S_NO", "DATE", "DUE_DATE", "TASK_NAME", "INITIAL_MAIL", "ALERT_MAIL", "STATUS", "PRIORITY"]:
                    if col_name in db_cols:
                        cell_values[col_name] = val

            for name, val in cell_values.items():
                col = db_cols.get(name)
                if col:
                    CellValue.objects.create(row=row, column=col, value=val, updated_by=request_user)

            # Create Task
            Task.objects.create(
                row=row,
                due_date=due_date,
                priority=priority.upper(),
                status=status_val.upper(),
                assigned_by=request_user
            )

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

        created_rows, err = self._import_rows_from_csv_data(file_data, table, request.user)
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

        created_rows, err = self._import_rows_from_csv_data(content, table, request.user)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": f"Successfully imported {len(created_rows)} rows from Google Sheets"}, status=status.HTTP_201_CREATED)

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
        
        # Verify DUE_DATE and TASK_NAME are present
        due_date_str = cells_data.get("DUE_DATE")
        task_name = cells_data.get("TASK_NAME")
        priority = cells_data.get("priority", "MEDIUM")

        if not due_date_str:
            return Response({"error": "DUE_DATE is mandatory"}, status=status.HTTP_400_BAD_REQUEST)
        if not task_name:
            return Response({"error": "TASK_NAME is mandatory"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            due_date = datetime.strptime(due_date_str.split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            return Response({"error": "Invalid DUE_DATE format. Use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)

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
                if column.name == "DUE_DATE":
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
                    if column.name == "DUE_DATE":
                        try:
                            task.due_date = datetime.strptime(str(value).split("T")[0], "%Y-%m-%d").date()
                            task.save(update_fields=["due_date"])
                        except ValueError:
                            pass

            # Sync with Task assigned_to if column data_type is USER or column name represents assignment
            col_name_upper = column.name.upper()
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
    return render(request, "tables/table_spreadsheet.html", {
        "table": table,
        "has_edit_access": has_edit
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

