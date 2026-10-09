import os, sys, time, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flowforce.settings")
import django; django.setup()

from django.test import TestCase
from django.contrib.auth import get_user_model
from datetime import date, timedelta
from employee_management.models import Department
from tables.models import Table, Column, Row, CellValue, TableAccess
from tasks.models import Task
from rest_framework import serializers
from tables.serializers import RowSerializer

User = get_user_model()

class TestSerializerOptimization(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Dept", slug="dept")
        self.user = User.objects.create_user(email="u@f.com", password="p", role="ADMIN", department=self.dept)
        self.user2 = User.objects.create_user(email="u2@f.com", password="p", role="EMPLOYEE", department=self.dept)
        self.table = Table.objects.create(name="T", job_type="LIST_PID", created_by=self.user, department=self.dept)
        TableAccess.objects.create(table=self.table, user=self.user, access_level="ADMIN")

        cols = list(Column.objects.filter(table=self.table).order_by("id"))
        today = date.today()

        rows = [Row(table=self.table, created_by=self.user) for _ in range(50)]
        Row.objects.bulk_create(rows)
        created_rows = list(Row.objects.filter(table=self.table).order_by("id"))

        tasks = []
        cells = []
        for i, r in enumerate(created_rows):
            t = Task(row=r, due_date=today, priority="HIGH", status="PENDING", assigned_by=self.user)
            tasks.append(t)
            for c in cols:
                cells.append(CellValue(row=r, column=c, value=f"Val {i} {c.name}"))

        CellValue.objects.bulk_create(cells)
        Task.objects.bulk_create(tasks)
        for t in Task.objects.filter(row__table=self.table):
            t.assigned_to.add(self.user2)

        self.rows_qs = list(Row.objects.filter(table=self.table, is_archived=False).select_related(
            'created_by', 'task', 'task__assigned_by'
        ).prefetch_related(
            'cells__column', 'cells__updated_by', 'task__assigned_to'
        )[:50])

    def test_compare_serializer_speed_and_exact_equality(self):
        # 1. Standard DRF RowSerializer
        t0 = time.perf_counter()
        std_data = RowSerializer(self.rows_qs, many=True).data
        t1 = time.perf_counter()
        std_time = (t1 - t0) * 1000

        dt_field = serializers.DateTimeField()
        format_dt = lambda dt: dt_field.to_representation(dt) if dt else None

        # 2. Optimized fast serializer representation
        def fast_serialize_row(row):
            # Task details
            task = getattr(row, "task", None)
            task_dict = None
            if task:
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

                task_dict = {
                    "id": task.id,
                    "row": row.id,
                    "assigned_by": task.assigned_by_id,
                    "assigned_by_detail": assigned_by_detail,
                    "assigned_to": assigned_to_ids,
                    "assigned_to_details": assigned_to_details,
                    "status": task.status,
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "priority": task.priority,
                    "created_at": format_dt(task.created_at),
                    "updated_at": format_dt(task.updated_at),
                }

            # Cells
            cells_list = []
            for cell in row.cells.all():
                cells_list.append({
                    "id": cell.id,
                    "row": cell.row_id,
                    "column": cell.column_id,
                    "column_name": cell.column.name,
                    "column_type": cell.column.data_type,
                    "value": cell.value,
                    "updated_by": cell.updated_by_id,
                    "updated_at": format_dt(cell.updated_at),
                })

            return {
                "id": row.id,
                "table": row.table_id,
                "created_by": row.created_by_id,
                "is_archived": row.is_archived,
                "created_at": format_dt(row.created_at),
                "updated_at": format_dt(row.updated_at),
                "cells": cells_list,
                "task_details": task_dict,
            }

        t2 = time.perf_counter()
        opt_data = [fast_serialize_row(r) for r in self.rows_qs]
        t3 = time.perf_counter()
        opt_time = (t3 - t2) * 1000

        print(f"\n[BENCHMARK] Standard RowSerializer:   {std_time:.2f} ms")
        print(f"[BENCHMARK] Optimized Representation: {opt_time:.2f} ms")
        print(f"[BENCHMARK] Speedup Factor:           {std_time / opt_time:.1f}x faster!")

        # Verify exact field structure and deep values match
        std_row = dict(std_data[0])
        opt_row = opt_data[0]
        self.assertEqual(std_row, opt_row)
        print("[VERIFICATION] std_row == opt_row DEEP EQUALITY 100% PASSED!")
