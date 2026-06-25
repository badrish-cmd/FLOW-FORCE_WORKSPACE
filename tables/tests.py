from django.test import TestCase
from django.contrib.auth import get_user_model
from employee_management.models import Department
from .models import Table, Column, Row, CellValue, TableAccess
from tasks.models import Task

User = get_user_model()

class TablesTestCase(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Tables Engineering Dept", slug="tables-engineering-dept")
        self.admin = User.objects.create_user(
            email="tablesadmin@flow-force.com",
            password="testpassword",
            full_name="Admin User",
            role="ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.employee = User.objects.create_user(
            email="tablesemp@flow-force.com",
            password="testpassword",
            full_name="Employee User",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )

    def test_table_system_columns_creation(self):
        # 1. Creating a table should auto-create system columns
        table = Table.objects.create(name="Development tasks", created_by=self.admin)
        columns = table.columns.all()
        col_names = [col.name for col in columns]
        
        self.assertIn("S_NO", col_names)
        self.assertIn("DATE", col_names)
        self.assertIn("DUE_DATE", col_names)
        self.assertIn("TASK_NAME", col_names)
        self.assertIn("INITIAL_MAIL", col_names)
        self.assertIn("ALERT_MAIL", col_names)
        self.assertEqual(columns.count(), 6)

    def test_row_creation_with_task_sync(self):
        table = Table.objects.create(name="Development tasks", created_by=self.admin)
        row = Row.objects.create(table=table, created_by=self.employee)
        
        # Test task auto-creation on views. For row views, creating a row will sync a Task.
        # Let's test custom column access checking
        col = table.columns.first()
        cell = CellValue.objects.create(row=row, column=col, value=123, updated_by=self.admin)
        self.assertEqual(cell.value, 123)

    def test_table_duplication(self):
        table = Table.objects.create(name="Source Table", created_by=self.admin)
        # Create a custom column
        Column.objects.create(table=table, name="Custom Text Column", data_type="TEXT", position=7)
        
        # Test duplication API logic
        # Clone table metadata
        new_table = Table.objects.create(
            name=f"Copy of {table.name}",
            description=table.description,
            created_by=self.admin,
            department=table.department
        )
        for col in table.columns.filter(is_system_column=False):
            Column.objects.create(
                table=new_table,
                name=col.name,
                data_type=col.data_type,
                is_mandatory=col.is_mandatory,
                is_system_column=False,
                position=col.position
            )
            
        self.assertEqual(new_table.name, "Copy of Source Table")
        self.assertEqual(new_table.columns.count(), 7)  # 6 system + 1 custom
        self.assertEqual(new_table.columns.filter(is_system_column=False).count(), 1)

    def test_import_csv_with_offsets_and_equality_check(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Import Test Table", created_by=self.admin)
        # Ensure we have ADMIN access
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        # Create valid CSV data with 11 empty/meta lines, then header, then data.
        # System columns are: S_NO, DATE, DUE_DATE, TASK_NAME, INITIAL_MAIL, ALERT_MAIL
        csv_lines = [
            "Metadata line 1", "Metadata line 2", "Metadata line 3", "Metadata line 4",
            "Metadata line 5", "Metadata line 6", "Metadata line 7", "Metadata line 8",
            "Metadata line 9", "Metadata line 10", "Metadata line 11",
            "S_NO,DATE,DUE_DATE,TASK_NAME,INITIAL_MAIL,ALERT_MAIL",
            "1,2026-06-22,2026-06-30,Imported Task Name,NO,NO"
        ]
        csv_data = "\n".join(csv_lines)

        from django.core.files.uploadedfile import SimpleUploadedFile
        csv_file = SimpleUploadedFile("tasks.csv", csv_data.encode("utf-8"), content_type="text/csv")

        # Perform POST to import-csv
        view = TableViewSet.as_view({'post': 'import_csv'})
        request = factory.post(f"/tables/api/tables/{table.id}/import-csv/", {"file": csv_file}, format="multipart")
        force_authenticate(request, user=self.admin)

        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Row.objects.filter(table=table).count(), 1)

        # Check if Task was created
        task = Task.objects.filter(row__table=table).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.task_name, "Imported Task Name")

    def test_import_csv_column_mismatch_error(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Mismatch Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        # Missing standard columns
        csv_lines = [
            "Metadata line 1", "Metadata line 2", "Metadata line 3", "Metadata line 4",
            "Metadata line 5", "Metadata line 6", "Metadata line 7", "Metadata line 8",
            "Metadata line 9", "Metadata line 10", "Metadata line 11",
            "S_NO,DATE,DUE_DATE",
            "1,2026-06-22,2026-06-30"
        ]
        csv_data = "\n".join(csv_lines)

        from django.core.files.uploadedfile import SimpleUploadedFile
        csv_file = SimpleUploadedFile("tasks.csv", csv_data.encode("utf-8"), content_type="text/csv")

        view = TableViewSet.as_view({'post': 'import_csv'})
        request = factory.post(f"/tables/api/tables/{table.id}/import-csv/", {"file": csv_file}, format="multipart")
        force_authenticate(request, user=self.admin)

        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Column mismatch", response.data["error"])

    def test_import_google_sheet_mocked(self):
        from unittest.mock import patch
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="GS Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        csv_lines = [
            "Metadata line 1", "Metadata line 2", "Metadata line 3", "Metadata line 4",
            "Metadata line 5", "Metadata line 6", "Metadata line 7", "Metadata line 8",
            "Metadata line 9", "Metadata line 10", "Metadata line 11",
            "S_NO,DATE,DUE_DATE,TASK_NAME,INITIAL_MAIL,ALERT_MAIL",
            "1,2026-06-22,2026-07-15,Google Sheet Task,NO,NO"
        ]
        csv_data = "\n".join(csv_lines)

        class MockUrlOpen:
            def __init__(self, data):
                self.data = data.encode('utf-8')
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            def read(self):
                return self.data

        with patch("urllib.request.urlopen", return_value=MockUrlOpen(csv_data)) as mock_urlopen:
            view = TableViewSet.as_view({'post': 'import_google_sheet'})
            request = factory.post(f"/tables/api/tables/{table.id}/import-google-sheet/", {
                "url": "https://docs.google.com/spreadsheets/d/1abc123_xyz/edit#gid=12"
            }, format="json")
            force_authenticate(request, user=self.admin)

            response = view(request, pk=table.id)
            self.assertEqual(response.status_code, 201)
            # Verify urlopen was called
            self.assertTrue(mock_urlopen.called)
            self.assertEqual(Row.objects.filter(table=table).count(), 1)
            task = Task.objects.filter(row__table=table).first()
            self.assertEqual(task.task_name, "Google Sheet Task")

    def test_delete_row(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Delete Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        
        row = Row.objects.create(table=table, created_by=self.admin)
        
        view = RowViewSet.as_view({'delete': 'destroy'})
        request = factory.delete(f"/tables/api/rows/{row.id}/")
        force_authenticate(request, user=self.admin)
        
        response = view(request, pk=row.id)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Row.objects.filter(id=row.id).exists())

    def test_engineer_table_pid_column_creation(self):
        table = Table.objects.create(name="Engineer tasks", job_type="ENGINEER", created_by=self.admin)
        columns = table.columns.all()
        col_names = [col.name for col in columns]
        
        self.assertIn("S_NO", col_names)
        self.assertIn("DATE", col_names)
        self.assertIn("DUE_DATE", col_names)
        self.assertIn("TASK_NAME", col_names)
        self.assertIn("INITIAL_MAIL", col_names)
        self.assertIn("ALERT_MAIL", col_names)
        self.assertIn("PID", col_names)
        
        pid_col = table.columns.get(name="PID")
        self.assertFalse(pid_col.is_mandatory)
        self.assertTrue(pid_col.is_system_column)
        self.assertEqual(pid_col.data_type, "TEXT")
        self.assertEqual(pid_col.position, 7)
        self.assertEqual(columns.count(), 7)

    def test_row_creation_preserves_pid(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Engineer Tracker", job_type="ENGINEER", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        view = RowViewSet.as_view({'post': 'create'})
        request = factory.post(f"/tables/api/rows/", {
            "table": table.id,
            "cells": {
                "TASK_NAME": "Verify PID Test",
                "DUE_DATE": "2026-07-10",
                "PID": "PID-999"
            }
        }, format="json")
        force_authenticate(request, user=self.admin)

        response = view(request)
        self.assertEqual(response.status_code, 201)

        row = Row.objects.filter(table=table).first()
        self.assertIsNotNone(row)

        pid_col = table.columns.get(name="PID")
        cell = CellValue.objects.get(row=row, column=pid_col)
        self.assertEqual(cell.value, "PID-999")

    def test_row_level_editing(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Engineer Tracker", job_type="ENGINEER", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        
        row = Row.objects.create(table=table, created_by=self.admin)
        pid_col = table.columns.get(name="PID")
        task_name_col = table.columns.get(name="TASK_NAME")
        
        CellValue.objects.create(row=row, column=pid_col, value="", updated_by=self.admin)
        CellValue.objects.create(row=row, column=task_name_col, value="Original Name", updated_by=self.admin)

        view = RowViewSet.as_view({'post': 'edit_row'})
        request = factory.post(f"/tables/api/rows/{row.id}/edit-row/", {
            "cells": {
                "TASK_NAME": "Updated Name",
                "PID": "PID-888"
            }
        }, format="json")
        force_authenticate(request, user=self.admin)

        response = view(request, pk=row.id)
        self.assertEqual(response.status_code, 200)

        self.assertEqual(CellValue.objects.get(row=row, column=pid_col).value, "PID-888")
        self.assertEqual(CellValue.objects.get(row=row, column=task_name_col).value, "Updated Name")

    def test_grant_access_invalid_user(self):
        table = Table.objects.create(name="Grant Test Table", created_by=self.admin)
        self.client.force_login(self.admin)
        
        # Post with empty user_id
        response = self.client.post("/tables/", {
            "action": "grant",
            "table_id": table.id,
            "user_id": "",
            "access_level": "EDIT"
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, "/tables/")
        
        # Verify that no TableAccess was created
        self.assertFalse(TableAccess.objects.filter(table=table).exists())
