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

class RowSerializer(serializers.ModelSerializer):
    cells = CellValueSerializer(many=True, read_only=True)
    task_details = serializers.SerializerMethodField()

    class Meta:
        model = Row
        fields = ["id", "table", "created_by", "is_archived", "created_at", "updated_at", "cells", "task_details"]

    def get_task_details(self, obj):
        if hasattr(obj, "task"):
            from tasks.serializers import TaskMinSerializer
            return TaskMinSerializer(obj.task).data
        return None

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
