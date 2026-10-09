import os
import sys
import time
import json
from datetime import date, timedelta
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flowforce.settings")
import django
django.setup()

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.db import connection
from django.core.cache import cache
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from employee_management.models import Department
from tables.models import Table, Column, Row, CellValue, TableAccess
from tasks.models import Task
from tables.views import RowViewSet
from tables.serializers import RowSerializer

User = get_user_model()

class MeasurePhaseABaseline(TestCase):
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        cls.dept = Department.objects.create(name="Engineering & Sales", slug="eng-sales")
        cls.admin = User.objects.create_user(
            email="perf_tester@flow-force.com",
            password="testpassword123",
            full_name="Perf Tester",
            role="ADMIN",
            department=cls.dept,
            status="APPROVED"
        )

        cls.user2 = User.objects.create_user(
            email="assignee@flow-force.com",
            password="testpassword123",
            full_name="Assignee User",
            role="EMPLOYEE",
            department=cls.dept,
            status="APPROVED"
        )

        # Create Table matching production LIST_PID (17 columns)
        cls.table = Table.objects.create(
            name="Production Master Table",
            job_type="LIST_PID",
            created_by=cls.admin,
            department=cls.dept
        )
        TableAccess.objects.create(table=cls.table, user=cls.admin, access_level="ADMIN")

        cols = list(cls.table.columns.order_by("position"))
        cls.columns = cols
        cls.col_map = {c.name: c for c in cols}

        # Populate realistic dataset: 3,729 rows to match production
        # Grouped Job Numbers (ENQUIRY_NO)
        today = date.today()
        TOTAL_ROWS = 3729
        print(f"\n[SETUP] Creating {TOTAL_ROWS} rows with 17 columns and grouped Job Numbers...")

        rows_to_create = [Row(table=cls.table, created_by=cls.admin) for _ in range(TOTAL_ROWS)]
        created_rows = Row.objects.bulk_create(rows_to_create)

        tasks_to_create = []
        cells_to_create = []
        statuses = ["PENDING", "IN_PROGRESS", "READY_FOR_REVIEW", "COMPLETED", "RETURNED"]
        priorities = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

        # Grouping patterns for Job numbers: cluster of 1 to 4 rows per job
        job_counter = 1
        job_remaining = 3

        for i, r in enumerate(created_rows):
            if job_remaining <= 0:
                job_counter += 1
                job_remaining = (i % 4) + 1  # 1 to 4 rows per job
            job_remaining -= 1

            enquiry_no = f"ENQ-2026-{job_counter:04d}"
            pid_no = f"PID-{job_counter:04d}"
            st = statuses[i % len(statuses)]
            pr = priorities[i % len(priorities)]
            dd = today + timedelta(days=(i % 30) - 10)

            t = Task(
                row=r,
                due_date=dd,
                priority=pr,
                status=st,
                assigned_by=cls.admin
            )
            tasks_to_create.append(t)

            # Cell values
            val_map = {
                "S_NO": i + 1,
                "ENQUIRY_NO/QUOTATION_NO": enquiry_no,
                "PID": pid_no,
                "NEW_PID_NO": f"NPID-{job_counter:04d}",
                "SALES_ORDER": f"SO-{i+1000}",
                "PO": f"PO-99{i%50}",
                "DATE": today.isoformat(),
                "FFE_SINGAPORE": "FFE-SG",
                "COMPANY_NAME": f"Customer Company {job_counter % 20}",
                "DESCRIPTION": f"Custom valve assembly specifications for project {job_counter}",
                "QTY": (i % 10) + 1,
                "DUE_DATE_CUSTOMER": (today + timedelta(days=5)).isoformat(),
                "DUE_DATE_FLOW_FORCE": dd.isoformat(),
                "PROJECT": f"Project Alpha {job_counter % 5}",
                "STATUS": st,
                "INITIAL_MAIL": "YES" if i % 2 == 0 else "NO",
                "ALERT_MAIL": "NO",
            }

            for col_name, val in val_map.items():
                if col_name in cls.col_map:
                    cells_to_create.append(CellValue(row=r, column=cls.col_map[col_name], value=val))

        Task.objects.bulk_create(tasks_to_create)
        # Assign user2 to every 3rd task
        created_tasks = list(Task.objects.filter(row__table=cls.table).order_by("id"))
        through_objs = []
        through_model = Task.assigned_to.through
        for idx, t in enumerate(created_tasks):
            if idx % 3 == 0:
                through_objs.append(through_model(task_id=t.id, employeeuser_id=cls.user2.id))
        through_model.objects.bulk_create(through_objs)

        # Batch insert cells in chunks of 5000
        BATCH_SIZE = 5000
        for b_idx in range(0, len(cells_to_create), BATCH_SIZE):
            CellValue.objects.bulk_create(cells_to_create[b_idx:b_idx+BATCH_SIZE])

        print(f"[SETUP] Finished creating {TOTAL_ROWS} rows, {len(cells_to_create)} cells, and tasks.")

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = RowViewSet.as_view({'get': 'list'})

    def test_measure_page_1(self):
        """Measure Page 1 (default page_size=50) with 3,729 rows in table"""
        req = self.factory.get(f"/tables/api/rows/?table={self.table.id}&page=1&page_size=50")
        force_authenticate(req, user=self.admin)

        # Warm-up run to exclude one-off python module load
        self.view(req)

        # Measured run
        t0 = time.perf_counter()
        with CaptureQueriesContext(connection) as ctx:
            res = self.view(req)
        t1 = time.perf_counter()

        elapsed_ms = (t1 - t0) * 1000
        query_count = len(ctx)
        db_time_ms = sum(float(q.get('time', 0)) for q in ctx.captured_queries) * 1000
        payload_json = json.dumps(res.data)
        payload_size_kb = len(payload_json.encode('utf-8')) / 1024

        print("\n" + "="*60)
        print("BASELINE MEASUREMENT — PAGE 1 (50 rows, 3,729 total rows)")
        print("="*60)
        print(f"Total API Latency:      {elapsed_ms:.2f} ms")
        print(f"SQL Query Count:        {query_count}")
        print(f"SQL Database Time:      {db_time_ms:.2f} ms")
        print(f"Python Processing Time: {elapsed_ms - db_time_ms:.2f} ms")
        print(f"JSON Payload Size:      {payload_size_kb:.2f} KB ({len(payload_json)} chars)")
        print(f"Total Rows Counted:     {res.data['count']}")
        print(f"Returned Rows in Page:  {len(res.data['results'])}")
        print("\nSQL Queries breakdown:")
        for idx, q in enumerate(ctx.captured_queries, 1):
            sql_snippet = q['sql'].strip()[:140].replace('\n', ' ')
            print(f"  {idx}. [{float(q['time'])*1000:.2f} ms] {sql_snippet}...")

    def test_measure_later_page(self):
        """Measure Page 50 (later page offset=2450) to evaluate pagination cost"""
        req = self.factory.get(f"/tables/api/rows/?table={self.table.id}&page=50&page_size=50")
        force_authenticate(req, user=self.admin)

        t0 = time.perf_counter()
        with CaptureQueriesContext(connection) as ctx:
            res = self.view(req)
        t1 = time.perf_counter()

        elapsed_ms = (t1 - t0) * 1000
        query_count = len(ctx)
        print("\n" + "="*60)
        print("BASELINE MEASUREMENT — LATER PAGE 50 (OFFSET 2450)")
        print("="*60)
        print(f"Total API Latency:      {elapsed_ms:.2f} ms")
        print(f"SQL Query Count:        {query_count}")
        print(f"Returned Rows in Page:  {len(res.data['results'])}")

    def test_measure_search_filter(self):
        """Measure search query performance across 3,729 rows"""
        req = self.factory.get(f"/tables/api/rows/?table={self.table.id}&page=1&page_size=50&search=Customer")
        force_authenticate(req, user=self.admin)

        t0 = time.perf_counter()
        with CaptureQueriesContext(connection) as ctx:
            res = self.view(req)
        t1 = time.perf_counter()

        elapsed_ms = (t1 - t0) * 1000
        query_count = len(ctx)
        print("\n" + "="*60)
        print("BASELINE MEASUREMENT — SEARCH FILTER ('Customer')")
        print("="*60)
        print(f"Total API Latency:      {elapsed_ms:.2f} ms")
        print(f"SQL Query Count:        {query_count}")
        print(f"Filtered Count:         {res.data['count']}")
        print(f"Returned Rows in Page:  {len(res.data['results'])}")

    def test_measure_serialization_component_cost(self):
        """Profile RowSerializer internal cost breakdown: cells vs task_details vs overhead"""
        page_qs = list(Row.objects.filter(table=self.table, is_archived=False).select_related(
            'created_by', 'task', 'task__assigned_by'
        ).prefetch_related(
            'cells__column', 'cells__updated_by', 'task__assigned_to'
        )[:50])

        t0 = time.perf_counter()
        serializer = RowSerializer(page_qs, many=True)
        data = serializer.data
        t1 = time.perf_counter()

        ser_time_ms = (t1 - t0) * 1000
        print("\n" + "="*60)
        print("SERIALIZATION COMPONENT COST (50 rows, 17 columns)")
        print("="*60)
        print(f"RowSerializer(50 rows).data Time: {ser_time_ms:.2f} ms")
        print(f"Average time per row serialized:  {ser_time_ms / 50:.3f} ms")

        # Inspect payload breakdown per row
        first_row = data[0]
        cells_size = len(json.dumps(first_row['cells']))
        task_size = len(json.dumps(first_row['task_details']))
        total_row_size = len(json.dumps(first_row))
        print(f"Single Row JSON Size:             {total_row_size} bytes")
        print(f"  - cells component:              {cells_size} bytes ({cells_size/total_row_size*100:.1f}%)")
        print(f"  - task_details component:       {task_size} bytes ({task_size/total_row_size*100:.1f}%)")
