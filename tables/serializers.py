from rest_framework import serializers
from .models import Table, Column, Row, CellValue, TableAccess, ColumnAccess
from auth_app.models import EmployeeUser
from employee_management.models import Department

class UserMinSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeUser
        fields = ["id", "full_name", "email", "role"]

class DepartmentMinSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "name", "color"]

class ColumnSerializer(serializers.ModelSerializer):
    class Meta:
        model = Column
        fields = ["id", "table", "name", "data_type", "is_mandatory", "is_system_column", "position", "options", "is_filterable"]
        read_only_fields = ["is_system_column"]

    def validate(self, attrs):
        table = attrs.get('table') or (self.instance.table if self.instance else None)
        name = attrs.get('name')
        if name and table:
            qs = Column.objects.filter(table=table, name__iexact=name.strip())
            if self.instance:
                qs = qs.exclude(id=self.instance.id)
            if qs.exists():
                raise serializers.ValidationError({"name": "A column with this name already exists in this table."})

        # Validate options if JSON config for TEXT
        data_type = attrs.get('data_type') or (self.instance.data_type if self.instance else 'TEXT')
        options = attrs.get('options')
        if data_type == 'TEXT' and options:
            import json
            if isinstance(options, str) and options.strip().startswith('{'):
                try:
                    cfg = json.loads(options)
                    if cfg.get('input_type') == 'multiline' or cfg.get('multiline') is True:
                        rows = cfg.get('rows')
                        if rows is not None:
                            try:
                                rows_int = int(rows)
                                if rows_int < 1:
                                    raise serializers.ValidationError({"options": "Rows must be at least 1."})
                            except (ValueError, TypeError):
                                raise serializers.ValidationError({"options": "Rows must be a valid number."})
                        max_length = cfg.get('max_length')
                        if max_length is not None and str(max_length).strip() != "":
                            try:
                                max_len_int = int(max_length)
                                if max_len_int < 1:
                                    raise serializers.ValidationError({"options": "Max length must be at least 1."})
                            except (ValueError, TypeError):
                                raise serializers.ValidationError({"options": "Max length must be a valid number."})
                except json.JSONDecodeError:
                    pass
        return attrs

class CellValueSerializer(serializers.ModelSerializer):
    column_name = serializers.CharField(source="column.name", read_only=True)
    column_type = serializers.CharField(source="column.data_type", read_only=True)

    class Meta:
        model = CellValue
        fields = ["id", "row", "column", "column_name", "column_type", "value", "updated_by", "updated_at"]

_DATETIME_FIELD = serializers.DateTimeField()

def _format_datetime(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    return _DATETIME_FIELD.to_representation(dt)

class RowSerializer(serializers.ModelSerializer):
    cells = CellValueSerializer(many=True, read_only=True)
    task_details = serializers.SerializerMethodField()

    class Meta:
        model = Row
        fields = ["id", "table", "created_by", "is_archived", "created_at", "updated_at", "cells", "task_details"]

    def _format_dt(self, dt):
        return _format_datetime(dt)

    def _serialize_task(self, task, row_id):
        if not task:
            return None
        assigned_by = task.assigned_by
        assigned_by_detail = {
            "id": assigned_by.id,
            "full_name": assigned_by.full_name,
            "email": assigned_by.email,
            "role": assigned_by.role,
        } if assigned_by else None

        assigned_to_ids = []
        assigned_to_details = []
        for emp in task.assigned_to.all():
            assigned_to_ids.append(emp.id)
            assigned_to_details.append({
                "id": emp.id,
                "full_name": emp.full_name,
                "email": emp.email,
                "role": emp.role,
            })

        return {
            "id": task.id,
            "row": row_id,
            "assigned_by": task.assigned_by_id,
            "assigned_by_detail": assigned_by_detail,
            "assigned_to": assigned_to_ids,
            "assigned_to_details": assigned_to_details,
            "status": task.status,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "priority": task.priority,
            "created_at": self._format_dt(task.created_at),
            "updated_at": self._format_dt(task.updated_at),
        }

    def get_task_details(self, obj):
        task = getattr(obj, "task", None)
        if not task:
            try:
                from tasks.models import Task
                from datetime import datetime
                due_date = None
                date_cell = obj.cells.filter(column__name__in=["DUE_DATE", "FOLLOW_UP_DATE", "RETURN_DATE", "DUE_DATE_FLOW_FORCE"]).first()
                if date_cell and date_cell.value:
                    try:
                        due_date = datetime.strptime(str(date_cell.value).split("T")[0], "%Y-%m-%d").date()
                    except Exception:
                        due_date = None
                task, _ = Task.objects.get_or_create(
                    row=obj,
                    defaults={
                        "due_date": due_date,
                        "priority": "MEDIUM",
                        "status": "PENDING",
                        "assigned_by": obj.created_by
                    }
                )
            except Exception:
                task = getattr(obj, "task", None)

        return self._serialize_task(task, obj.id)

    def to_representation(self, instance):
        # 1. Fast task serialization
        task = getattr(instance, "task", None)
        if task is None:
            task_dict = self.get_task_details(instance)
        else:
            task_dict = self._serialize_task(task, instance.id)

        # 2. Fast cells serialization
        cells_list = []
        for cell in instance.cells.all():
            col = cell.column
            cells_list.append({
                "id": cell.id,
                "row": cell.row_id,
                "column": cell.column_id,
                "column_name": col.name if col else None,
                "column_type": col.data_type if col else None,
                "value": cell.value,
                "updated_by": cell.updated_by_id,
                "updated_at": self._format_dt(cell.updated_at),
            })

        return {
            "id": instance.id,
            "table": instance.table_id,
            "created_by": instance.created_by_id,
            "is_archived": instance.is_archived,
            "created_at": self._format_dt(instance.created_at),
            "updated_at": self._format_dt(instance.updated_at),
            "cells": cells_list,
            "task_details": task_dict,
        }

class TableSerializer(serializers.ModelSerializer):
    columns = ColumnSerializer(many=True, read_only=True)
    created_by_detail = UserMinSerializer(source="created_by", read_only=True)
    department_detail = DepartmentMinSerializer(source="department", read_only=True)

    class Meta:
        model = Table
        fields = ["id", "name", "description", "created_by", "created_by_detail", "department", "department_detail", "is_active", "job_type", "created_at", "updated_at", "columns"]

class TableAccessSerializer(serializers.ModelSerializer):
    user_detail = UserMinSerializer(source="user", read_only=True)
    department_detail = DepartmentMinSerializer(source="department", read_only=True)

    class Meta:
        model = TableAccess
        fields = ["id", "table", "user", "user_detail", "department", "department_detail", "access_level", "created_at", "updated_at"]

class ColumnAccessSerializer(serializers.ModelSerializer):
    user_detail = UserMinSerializer(source="user", read_only=True)

    class Meta:
        model = ColumnAccess
        fields = ["id", "column", "user", "user_detail", "access_level", "created_at", "updated_at"]
