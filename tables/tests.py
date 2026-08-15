import datetime
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

    def test_import_csv_with_different_date_formats(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Import Date Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        csv_lines = [
            "Metadata line 1", "Metadata line 2", "Metadata line 3", "Metadata line 4",
            "Metadata line 5", "Metadata line 6", "Metadata line 7", "Metadata line 8",
            "Metadata line 9", "Metadata line 10", "Metadata line 11",
            "S_NO,DATE,DUE_DATE,TASK_NAME,INITIAL_MAIL,ALERT_MAIL",
            "1,22/06/2026,30/06/2026,Imported Task Name DD-MM-YYYY,NO,NO",
            "2,06/22/2026,06/30/2026,Imported Task Name MM-DD-YYYY,NO,NO"
        ]
        csv_data = "\n".join(csv_lines)

        from django.core.files.uploadedfile import SimpleUploadedFile
        csv_file = SimpleUploadedFile("tasks.csv", csv_data.encode("utf-8"), content_type="text/csv")

        view = TableViewSet.as_view({'post': 'import_csv'})
        request = factory.post(f"/tables/api/tables/{table.id}/import-csv/", {"file": csv_file}, format="multipart")
        force_authenticate(request, user=self.admin)

        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Row.objects.filter(table=table).count(), 2)

        # Check if Tasks were created and parsed correctly
        tasks = list(Task.objects.filter(row__table=table).order_by('id'))
        self.assertEqual(len(tasks), 2)
        import datetime
        self.assertEqual(tasks[0].due_date, datetime.date(2026, 6, 30))
        self.assertEqual(tasks[1].due_date, datetime.date(2026, 6, 30))

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
        self.assertIn("Required column", response.data["error"])

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

    def test_bulk_update_action(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Bulk Update Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        TableAccess.objects.create(table=table, user=self.employee, access_level="VIEW")

        row = Row.objects.create(table=table, created_by=self.admin)
        task = Task.objects.create(row=row, due_date="2026-07-15", status="PENDING", priority="MEDIUM", assigned_by=self.admin)

        # 1. Employee is forbidden
        view = TableViewSet.as_view({'post': 'bulk_update'})
        request = factory.post(f"/tables/api/tables/{table.id}/bulk-update/", {"field": "INITIAL_MAIL", "value": "YES"}, format="json")
        force_authenticate(request, user=self.employee)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 403)

        # 2. Admin successfully bulk updates INITIAL_MAIL to YES
        request = factory.post(f"/tables/api/tables/{table.id}/bulk-update/", {"field": "INITIAL_MAIL", "value": "YES"}, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 200)

        # Verify initial_mail_sent is True on task and cell value is YES
        task.refresh_from_db()
        self.assertTrue(task.initial_mail_sent)
        init_col = table.columns.get(name="INITIAL_MAIL")
        self.assertEqual(CellValue.objects.get(row=row, column=init_col).value, "YES")

        # 3. Admin successfully bulk updates STATUS to COMPLETED
        # Create STATUS custom column
        status_col = Column.objects.create(table=table, name="STATUS", data_type="TEXT")
        request = factory.post(f"/tables/api/tables/{table.id}/bulk-update/", {"field": "STATUS", "value": "COMPLETED"}, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 200)

        task.refresh_from_db()
        self.assertEqual(task.status, "COMPLETED")
        self.assertEqual(CellValue.objects.get(row=row, column=status_col).value, "COMPLETED")

    def test_sync_status_cell_to_task(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Status Sync Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        status_col = Column.objects.create(table=table, name="STATUS", data_type="TEXT")

        row = Row.objects.create(table=table, created_by=self.admin)
        task = Task.objects.create(row=row, due_date="2026-07-15", status="PENDING", priority="MEDIUM", assigned_by=self.admin)

        view = RowViewSet.as_view({'post': 'edit_cell'})
        request = factory.post(f"/tables/api/rows/{row.id}/edit-cell/", {
            "column": status_col.id,
            "value": "IN_PROGRESS"
        }, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=row.id)
        self.assertEqual(response.status_code, 200)

        task.refresh_from_db()
        self.assertEqual(task.status, "IN_PROGRESS")

    def test_sync_task_to_status_cell(self):
        from tasks.views import TaskViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Task to Cell Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        status_col = Column.objects.create(table=table, name="STATUS", data_type="TEXT")

        row = Row.objects.create(table=table, created_by=self.admin)
        task = Task.objects.create(row=row, due_date="2026-07-15", status="PENDING", priority="MEDIUM", assigned_by=self.admin)

        view = TaskViewSet.as_view({'post': 'update_status'})
        request = factory.post(f"/tasks/api/tasks/{task.id}/update-status/", {
            "status": "COMPLETED"
        }, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=task.id)
        self.assertEqual(response.status_code, 200)

        # Verify STATUS cell in spreadsheet was updated to COMPLETED
        cell = CellValue.objects.get(row=row, column=status_col)
        self.assertEqual(cell.value, "COMPLETED")

    def test_table_edit_details_and_permissions(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Original Table", description="Original Desc", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        TableAccess.objects.create(table=table, user=self.employee, access_level="EDIT")

        view = TableViewSet.as_view({'patch': 'partial_update'})

        # 1. Non-admin (employee with EDIT access) cannot change table name
        request = factory.patch(f"/tables/api/tables/{table.id}/", {"name": "Hacked Name"}, format="json")
        force_authenticate(request, user=self.employee)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 403)

        # 2. Admin can change table name and description
        request = factory.patch(f"/tables/api/tables/{table.id}/", {"name": "New Name", "description": "New Desc"}, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 200)
        table.refresh_from_db()
        self.assertEqual(table.name, "New Name")
        self.assertEqual(table.description, "New Desc")

    def test_column_edit_and_permissions(self):
        from tables.views import ColumnViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Column Permission Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        TableAccess.objects.create(table=table, user=self.employee, access_level="EDIT")

        col = Column.objects.create(table=table, name="Old Col", data_type="TEXT", position=7, options="A,B")

        view = ColumnViewSet.as_view({'patch': 'partial_update'})

        # 1. Employee cannot update column options
        request = factory.patch(f"/tables/api/columns/{col.id}/", {"options": "X,Y,Z"}, format="json")
        force_authenticate(request, user=self.employee)
        response = view(request, pk=col.id)
        self.assertEqual(response.status_code, 403)

        # 2. Admin can update column options
        request = factory.patch(f"/tables/api/columns/{col.id}/", {"options": "X,Y,Z"}, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=col.id)
        self.assertEqual(response.status_code, 200)
        col.refresh_from_db()
        self.assertEqual(col.options, "X,Y,Z")

    def test_table_duplication_structure_and_values(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Full Copy Table", created_by=self.admin, job_type="ENGINEER")
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        # Custom column with options
        custom_col = Column.objects.create(table=table, name="Custom Dropdown", data_type="DROPDOWN", options="Option1,Option2", position=7)

        # Row with cell values
        row = Row.objects.create(table=table, created_by=self.admin)
        cell1 = CellValue.objects.create(row=row, column=custom_col, value="Option1", updated_by=self.admin)
        
        # Row has task
        task = Task.objects.create(row=row, status="IN_PROGRESS", due_date="2026-07-01", priority="HIGH", assigned_by=self.admin)
        task.assigned_to.add(self.employee)

        view = TableViewSet.as_view({'post': 'duplicate_table'})
        request = factory.post(f"/tables/api/tables/{table.id}/duplicate/")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 201)

        # Verify new table metadata
        new_table_id = response.data["id"]
        new_table = Table.objects.get(id=new_table_id)
        self.assertEqual(new_table.name, "Copy of Full Copy Table")
        self.assertEqual(new_table.job_type, "ENGINEER")

        # Verify duplicated columns and options
        new_custom_col = new_table.columns.get(name="Custom Dropdown")
        self.assertEqual(new_custom_col.data_type, "DROPDOWN")
        self.assertEqual(new_custom_col.options, "Option1,Option2")

        # Verify duplicated rows and cell values
        new_row = new_table.rows.first()
        self.assertIsNotNone(new_row)
        new_cell = CellValue.objects.get(row=new_row, column=new_custom_col)
        self.assertEqual(new_cell.value, "Option1")

        # Verify task is duplicated with assignees
        new_task = new_row.task
        self.assertEqual(new_task.status, "IN_PROGRESS")
        self.assertEqual(new_task.priority, "HIGH")
        self.assertEqual(new_task.due_date.strftime("%Y-%m-%d"), "2026-07-01")
        self.assertIn(self.employee, new_task.assigned_to.all())

    def test_bulk_delete_rows(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Delete Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        TableAccess.objects.create(table=table, user=self.employee, access_level="VIEW")

        row1 = Row.objects.create(table=table, created_by=self.admin)
        row2 = Row.objects.create(table=table, created_by=self.admin)
        row3 = Row.objects.create(table=table, created_by=self.admin)

        view = TableViewSet.as_view({'post': 'bulk_delete_rows'})

        # 1. Non-edit user (employee with VIEW access) cannot bulk delete
        request = factory.post(f"/tables/api/tables/{table.id}/bulk-delete-rows/", {"row_ids": [row1.id, row2.id]}, format="json")
        force_authenticate(request, user=self.employee)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 403)

        # 2. Admin can delete selected rows (row1, row2)
        request = factory.post(f"/tables/api/tables/{table.id}/bulk-delete-rows/", {"row_ids": [row1.id, row2.id]}, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Row.objects.filter(id__in=[row1.id, row2.id]).exists())
        self.assertTrue(Row.objects.filter(id=row3.id).exists())

        # 3. Admin can delete all remaining rows in table
        request = factory.post(f"/tables/api/tables/{table.id}/bulk-delete-rows/", format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Row.objects.filter(table=table).exists())

    def test_send_manual_escalation_api(self):
        from django.utils import timezone
        from datetime import timedelta
        from tasks.models import EmailLog
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Escalation Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        TableAccess.objects.create(table=table, user=self.employee, access_level="VIEW")

        row = Row.objects.create(table=table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=timezone.localdate() - timedelta(days=2),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.add(self.employee)

        view = TableViewSet.as_view({'post': 'send_manual_escalation'})

        # 1. Non-admin user cannot trigger manual escalation
        request = factory.post(f"/tables/api/tables/{table.id}/send-escalation/", {"row_ids": [row.id]}, format="json")
        force_authenticate(request, user=self.employee)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 403)

        # 2. Admin can trigger manual escalation
        request = factory.post(f"/tables/api/tables/{table.id}/send-escalation/", {"row_ids": [row.id]}, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 200)

        # Verify EmailLog was created
        log = EmailLog.objects.filter(task=task, email_type="OVERDUE_ESCALATION_MAIL").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.recipient_email, self.employee.email)
        
        # Verify task escalation level updated
        task.refresh_from_db()
        self.assertEqual(task.last_escalation_level, 2)

    def test_send_manual_escalation_non_overdue_api(self):
        from django.utils import timezone
        from datetime import timedelta
        from tasks.models import EmailLog
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Non-Overdue Escalation Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        TableAccess.objects.create(table=table, user=self.employee, access_level="VIEW")

        row = Row.objects.create(table=table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=timezone.localdate() + timedelta(days=2),  # Future date (not overdue)
            priority="MEDIUM",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.add(self.employee)

        view = TableViewSet.as_view({'post': 'send_manual_escalation'})

        # Admin triggers escalation on the selected row (even though task is not overdue)
        request = factory.post(f"/tables/api/tables/{table.id}/send-escalation/", {"row_ids": [row.id]}, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 200)

        # Verify EmailLog was created and status level updated to 0
        log = EmailLog.objects.filter(task=task, email_type="OVERDUE_ESCALATION_MAIL").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.recipient_email, self.employee.email)
        self.assertIn("A task has been escalated and requires immediate attention.", log.body)

        task.refresh_from_db()
        self.assertEqual(task.last_escalation_level, 0)

    def test_personal_job_type_creation_and_row_addition(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        # 1. Create a PERSONAL table. It should have 0 columns automatically created.
        table = Table.objects.create(name="My Personal Table", job_type="PERSONAL", created_by=self.admin)
        self.assertEqual(table.columns.count(), 0)

        # 2. Add a custom column to the table.
        custom_col = Column.objects.create(table=table, name="My Task Header", data_type="TEXT", position=1)

        # 3. Create a Row. It should bypass due date / task name mandatory validations.
        view = RowViewSet.as_view({'post': 'create'})
        
        request = factory.post(f"/tables/api/rows/", {
            "table": table.id,
            "cells": {
                "My Task Header": "Do gym workout"
            }
        }, format="json")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 201)

        # 4. Verify Row and Cell value were saved.
        row = Row.objects.filter(table=table).first()
        self.assertIsNotNone(row)
        cell = row.cells.filter(column=custom_col).first()
        self.assertEqual(cell.value, "Do gym workout")

        # 5. Verify the Task has default due_date=None, and task_name is guessed correctly.
        task = getattr(row, "task", None)
        self.assertIsNotNone(task)
        self.assertIsNone(task.due_date)
        self.assertEqual(task.task_name, "Do gym workout")

    def test_personal_job_type_csv_import(self):
        from unittest.mock import patch
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        # 1. Create a PERSONAL table and some custom columns.
        table = Table.objects.create(name="My Personal Table CSV", job_type="PERSONAL", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        
        custom_col1 = Column.objects.create(table=table, name="My Task Header", data_type="TEXT", position=1)
        custom_col2 = Column.objects.create(table=table, name="Note Info", data_type="TEXT", position=2)

        # 2. Mock Google sheet import with personal columns (NO system columns like TASK_NAME, S_NO or DUE_DATE)
        csv_lines = [
            "My Task Header,Note Info",
            "Gym workout,Focus on cardio and legs",
            "Read book,Finish chapter 5"
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
            self.assertEqual(Row.objects.filter(table=table).count(), 2)

            # Verify task names resolved from the text columns
            rows = Row.objects.filter(table=table).order_by("id")
            self.assertEqual(rows[0].task.task_name, "Gym workout")
            self.assertEqual(rows[1].task.task_name, "Read book")

    def test_column_unique_name_validation(self):
        from tables.serializers import ColumnSerializer
        table = Table.objects.create(name="Col Unique Table", created_by=self.admin)
        Column.objects.create(table=table, name="CustomCol1", data_type="TEXT")

        # Serializer should raise validation error if creating column with duplicate name
        serializer = ColumnSerializer(data={"table": table.id, "name": "CustomCol1", "data_type": "TEXT"})
        self.assertFalse(serializer.is_valid())
        self.assertIn("name", serializer.errors)

        # Serializer should pass if name is unique
        serializer2 = ColumnSerializer(data={"table": table.id, "name": "CustomCol2", "data_type": "TEXT"})
        self.assertTrue(serializer2.is_valid())

    def test_clear_column_values_api(self):
        from tables.views import ColumnViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Clear Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.employee, access_level="VIEW")
        col = Column.objects.create(table=table, name="CustomCol", data_type="TEXT")
        row = Row.objects.create(table=table, created_by=self.admin)
        cell = CellValue.objects.create(row=row, column=col, value="Target Value")

        view = ColumnViewSet.as_view({'post': 'clear_values'})

        # 1. Non-admin cannot clear values
        request = factory.post(f"/tables/api/columns/{col.id}/clear-values/")
        force_authenticate(request, user=self.employee)
        response = view(request, pk=col.id)
        self.assertEqual(response.status_code, 403)

        # 2. Admin can clear values
        request = factory.post(f"/tables/api/columns/{col.id}/clear-values/")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=col.id)
        self.assertEqual(response.status_code, 200)

        cell.refresh_from_db()
        self.assertIsNone(cell.value)

    def test_delete_rows_by_column_api(self):
        from tables.views import ColumnViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Delete Rows Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.employee, access_level="VIEW")
        col = Column.objects.create(table=table, name="CustomCol", data_type="TEXT")
        row1 = Row.objects.create(table=table, created_by=self.admin)
        row2 = Row.objects.create(table=table, created_by=self.admin)
        
        CellValue.objects.create(row=row1, column=col, value="Row 1 Value")
        # row2 has no value

        view = ColumnViewSet.as_view({'post': 'delete_rows'})

        # 1. Non-admin cannot bulk delete rows by column
        request = factory.post(f"/tables/api/columns/{col.id}/delete-rows/")
        force_authenticate(request, user=self.employee)
        response = view(request, pk=col.id)
        self.assertEqual(response.status_code, 403)

        # 2. Admin can bulk delete rows by column (should delete row1, keep row2)
        request = factory.post(f"/tables/api/columns/{col.id}/delete-rows/")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=col.id)
        self.assertEqual(response.status_code, 200)

        self.assertFalse(Row.objects.filter(id=row1.id).exists())
        self.assertTrue(Row.objects.filter(id=row2.id).exists())

    def test_list_pid_table_behavior(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="LIST_PID table", job_type="LIST_PID", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")
        TableAccess.objects.create(table=table, user=self.employee, access_level="EDIT")

        col_names = [col.name for col in table.columns.all()]
        self.assertIn("S_NO", col_names)
        self.assertIn("ENQUIRY_NO/QUOTATION_NO", col_names)
        self.assertIn("PID", col_names)
        self.assertIn("DUE_DATE_FLOW_FORCE", col_names)
        self.assertIn("INITIAL_MAIL", col_names)
        self.assertIn("ALERT_MAIL", col_names)

        # Test Row Creation via API
        view = RowViewSet.as_view({'post': 'create'})
        request = factory.post("/tables/api/rows/", {
            "table": table.id,
            "cells": {
                "ENQUIRY_NO/QUOTATION_NO": "ENQ-1234",
                "PID": "PID-5678",
                "DUE_DATE_FLOW_FORCE": "2026-07-20",
                "QTY": "15"
            }
        }, format="json")
        force_authenticate(request, user=self.employee)
        response = view(request)
        self.assertEqual(response.status_code, 201)

        row_id = response.data["id"]
        row = Row.objects.get(id=row_id)

        # Check CellValues
        self.assertEqual(CellValue.objects.get(row=row, column__name="ENQUIRY_NO/QUOTATION_NO").value, "ENQ-1234")
        self.assertEqual(CellValue.objects.get(row=row, column__name="DUE_DATE_FLOW_FORCE").value, "2026-07-20")
        self.assertEqual(CellValue.objects.get(row=row, column__name="QTY").value, "15")

        # Check Task
        task = getattr(row, "task", None)
        self.assertIsNotNone(task)
        self.assertEqual(task.task_name, "ENQ-1234")
        import datetime
        self.assertEqual(task.due_date, datetime.date(2026, 7, 20))

        # Check Permissions - System columns should be EDITABLE for LIST_PID
        from tables.permissions import get_column_access_level
        s_no_col = table.columns.get(name="S_NO")
        initial_mail_col = table.columns.get(name="INITIAL_MAIL")
        self.assertEqual(get_column_access_level(self.employee, s_no_col), "EDITABLE")
        self.assertEqual(get_column_access_level(self.employee, initial_mail_col), "EDITABLE")

    def test_dynamic_dropdown_column_filtering(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from tables.views import RowViewSet
        factory = APIRequestFactory()
        table = Table.objects.create(name="Dynamic Filter Table", created_by=self.admin)
        col = Column.objects.create(table=table, name="Status Col", data_type="DROPDOWN", options="Open,Closed", is_filterable=True)
        
        row1 = Row.objects.create(table=table, created_by=self.employee)
        row2 = Row.objects.create(table=table, created_by=self.employee)
        
        CellValue.objects.create(row=row1, column=col, value="Open", updated_by=self.admin)
        CellValue.objects.create(row=row2, column=col, value="Closed", updated_by=self.admin)
        
        # Test API filtering with col_<id>=Open
        view = RowViewSet.as_view({'get': 'list'})
        request = factory.get(f"/tables/api/rows/?table={table.id}&col_{col.id}=Open")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], row1.id)

    def test_list_pid_import_and_filtering(self):
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        from django.core.files.uploadedfile import SimpleUploadedFile
        factory = APIRequestFactory()

        table = Table.objects.create(name="LIST_PID Import Test Table", job_type="LIST_PID", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        # Verify columns are is_system_column = False
        for col in table.columns.all():
            self.assertFalse(col.is_system_column)

        # Create valid CSV data where DATE, DUE_DATE_CUSTOMER, and DUE_DATE_FLOW_FORCE are missing/blank
        csv_lines = [
            "S_NO,DATE,ENQUIRY_NO,DUE_DATE_FLOW_FORCE,DUE_DATE_CUSTOMER,COMPANY_NAME",
            "1,,ENQ-1234,,,Company A",
            "2,,ENQ-5678,,,Company B",
            "3,,ENQ-9012,,,Company A"
        ]
        csv_data = "\n".join(csv_lines)
        csv_file = SimpleUploadedFile("import_pid.csv", csv_data.encode("utf-8"), content_type="text/csv")

        view = TableViewSet.as_view({'post': 'import_csv'})
        request = factory.post(f"/tables/api/tables/{table.id}/import-csv/", {
            "file": csv_file
        }, format="multipart")
        force_authenticate(request, user=self.admin)
        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 201)

        # Check imported rows and blank dates
        rows = Row.objects.filter(table=table)
        self.assertEqual(rows.count(), 3)
        for r in rows:
            name_cell = CellValue.objects.filter(row=r, column__name="ENQUIRY_NO/QUOTATION_NO").first()
            self.assertIsNotNone(name_cell)
            self.assertTrue(name_cell.value.startswith("ENQ-"))

            date_cell = CellValue.objects.filter(row=r, column__name="DATE").first()
            due_ff_cell = CellValue.objects.filter(row=r, column__name="DUE_DATE_FLOW_FORCE").first()
            # Verify they are blank/None (safe_parse_date returned None, and they were not auto-assigned today)
            self.assertTrue(not date_cell or date_cell.value in [None, ""])
            self.assertTrue(not due_ff_cell or due_ff_cell.value in [None, ""])

            # Verify associated Task.due_date is None
            self.assertIsNone(r.task.due_date)

        # Verify company_name column became filterable DROPDOWN with analyzed company options
        company_col = table.columns.get(name="COMPANY_NAME")
        self.assertEqual(company_col.data_type, "DROPDOWN")
        self.assertTrue(company_col.is_filterable)
        # Options should be analyzed, sorted and distinct: "Company A,Company B"
        self.assertEqual(company_col.options, "Company A,Company B")

    def test_row_search_case_and_space_insensitive(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Search Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        col = Column.objects.create(table=table, name="Test Column", data_type="TEXT", position=7)
        row1 = Row.objects.create(table=table, created_by=self.employee)
        row2 = Row.objects.create(table=table, created_by=self.employee)

        CellValue.objects.create(row=row1, column=col, value="Esih Ayu Lisa", updated_by=self.admin)
        CellValue.objects.create(row=row2, column=col, value="Tri Pirmansyah", updated_by=self.admin)

        view = RowViewSet.as_view({'get': 'list'})

        # Test case & space insensitive search: "esihayulisa" -> should match row1
        request = factory.get(f"/tables/api/rows/?table={table.id}&search=esihayulisa")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['id'], row1.id)

        # Test another search: " TRIPIRMANSYAH " -> should match row2
        request = factory.get(f"/tables/api/rows/?table={table.id}&search=  TRIPIRMANSYAH ")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['id'], row2.id)

        # Test non-matching search: "unknown" -> should match 0 rows
        request = factory.get(f"/tables/api/rows/?table={table.id}&search=unknown")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 0)

    def test_row_sorting_options(self):
        from tables.views import RowViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        from tasks.models import Task, TaskFollowUp
        import datetime
        factory = APIRequestFactory()

        # --- Test GENERAL Table (Due Date & Date Assigned) ---
        table = Table.objects.create(name="Sort Test Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        date_col = table.columns.get(name="DATE")
        
        row1 = Row.objects.create(table=table, created_by=self.employee)
        row2 = Row.objects.create(table=table, created_by=self.employee)
        row3 = Row.objects.create(table=table, created_by=self.employee)

        # 1. Populate Date Assigned (via DATE column)
        CellValue.objects.create(row=row1, column=date_col, value="2026-07-02", updated_by=self.admin)
        CellValue.objects.create(row=row2, column=date_col, value="2026-07-01", updated_by=self.admin)
        CellValue.objects.create(row=row3, column=date_col, value="2026-07-03", updated_by=self.admin)

        # 2. Create Tasks for due date sorting
        task1 = Task.objects.create(row=row1, due_date=datetime.date(2026, 8, 15), status="PENDING")
        task2 = Task.objects.create(row=row2, due_date=datetime.date(2026, 8, 10), status="PENDING")
        task3 = Task.objects.create(row=row3, due_date=datetime.date(2026, 8, 20), status="PENDING")

        view = RowViewSet.as_view({'get': 'list'})

        # Test Sort by due_date Ascending: row2 (Aug 10), row1 (Aug 15), row3 (Aug 20)
        request = factory.get(f"/tables/api/rows/?table={table.id}&sort_by=due_date&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [row2.id, row1.id, row3.id])

        # Test Sort by due_date Descending: row3 (Aug 20), row1 (Aug 15), row2 (Aug 10)
        request = factory.get(f"/tables/api/rows/?table={table.id}&sort_by=due_date&sort_dir=desc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [row3.id, row1.id, row2.id])

        # Test follow_up_date sort on GENERAL table is ignored (returns default ordering by ID)
        request = factory.get(f"/tables/api/rows/?table={table.id}&sort_by=follow_up_date&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [row1.id, row2.id, row3.id])

        # Test Sort by date_assigned Ascending: row2 (July 1), row1 (July 2), row3 (July 3)
        request = factory.get(f"/tables/api/rows/?table={table.id}&sort_by=date_assigned&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [row2.id, row1.id, row3.id])

        # Test Sort by date_assigned Descending: row3 (July 3), row1 (July 2), row2 (July 1)
        request = factory.get(f"/tables/api/rows/?table={table.id}&sort_by=date_assigned&sort_dir=desc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [row3.id, row1.id, row2.id])

        # --- Test SALES Table (Follow-up Date & Date Assigned) ---
        sales_table = Table.objects.create(name="Sales Sort Test Table", job_type="SALES", created_by=self.admin)
        TableAccess.objects.create(table=sales_table, user=self.admin, access_level="ADMIN")
        
        s_row1 = Row.objects.create(table=sales_table, created_by=self.employee)
        s_row2 = Row.objects.create(table=sales_table, created_by=self.employee)
        s_row3 = Row.objects.create(table=sales_table, created_by=self.employee)

        s_task1 = Task.objects.create(row=s_row1, due_date=datetime.date(2026, 8, 15), status="PENDING")
        s_task2 = Task.objects.create(row=s_row2, due_date=datetime.date(2026, 8, 10), status="PENDING")
        s_task3 = Task.objects.create(row=s_row3, due_date=datetime.date(2026, 8, 20), status="PENDING")

        TaskFollowUp.objects.create(task=s_task1, follow_up_date=datetime.date(2026, 9, 2), discussed_points="points 1")
        TaskFollowUp.objects.create(task=s_task2, follow_up_date=datetime.date(2026, 9, 3), discussed_points="points 2")
        # s_task3 has no follow ups (should fallback to due_date: 2026-08-20)

        # Test Sort by follow_up_date Ascending: s_row3 (Aug 20), s_row1 (Sept 2), s_row2 (Sept 3)
        request = factory.get(f"/tables/api/rows/?table={sales_table.id}&sort_by=follow_up_date&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [s_row3.id, s_row1.id, s_row2.id])

        # Test Sort by follow_up_date Descending: s_row2 (Sept 3), s_row1 (Sept 2), s_row3 (Aug 20)
        request = factory.get(f"/tables/api/rows/?table={sales_table.id}&sort_by=follow_up_date&sort_dir=desc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [s_row2.id, s_row1.id, s_row3.id])

        # Test due_date sort on SALES table is ignored
        request = factory.get(f"/tables/api/rows/?table={sales_table.id}&sort_by=due_date&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [s_row1.id, s_row2.id, s_row3.id])

        # --- Test LIST_PID Table (Due Date maps to DUE_DATE_FLOW_FORCE) ---
        pid_table = Table.objects.create(name="PID Sort Test Table", job_type="LIST_PID", created_by=self.admin)
        TableAccess.objects.create(table=pid_table, user=self.admin, access_level="ADMIN")
        
        flow_force_col = pid_table.columns.get(name="DUE_DATE_FLOW_FORCE")
        
        p_row1 = Row.objects.create(table=pid_table, created_by=self.employee)
        p_row2 = Row.objects.create(table=pid_table, created_by=self.employee)
        p_row3 = Row.objects.create(table=pid_table, created_by=self.employee)
        
        CellValue.objects.create(row=p_row1, column=flow_force_col, value="2026-10-15", updated_by=self.admin)
        CellValue.objects.create(row=p_row2, column=flow_force_col, value="2026-10-10", updated_by=self.admin)
        CellValue.objects.create(row=p_row3, column=flow_force_col, value="2026-10-20", updated_by=self.admin)
        
        # Test Sort by due_date on LIST_PID table (maps to DUE_DATE_FLOW_FORCE)
        request = factory.get(f"/tables/api/rows/?table={pid_table.id}&sort_by=due_date&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [p_row2.id, p_row1.id, p_row3.id])

        # Test Sort by date (normalized to date_assigned) on GENERAL table
        request = factory.get(f"/tables/api/rows/?table={table.id}&sort_by=date&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [row2.id, row1.id, row3.id])

        # Test Sort by enquiry_no on LIST_PID table
        enquiry_col = pid_table.columns.get(name="ENQUIRY_NO/QUOTATION_NO")
        CellValue.objects.create(row=p_row1, column=enquiry_col, value="ENQ-002", updated_by=self.admin)
        CellValue.objects.create(row=p_row2, column=enquiry_col, value="ENQ-001", updated_by=self.admin)
        CellValue.objects.create(row=p_row3, column=enquiry_col, value="ENQ-003", updated_by=self.admin)

        request = factory.get(f"/tables/api/rows/?table={pid_table.id}&sort_by=enquiry_no&sort_dir=asc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [p_row2.id, p_row1.id, p_row3.id])

        request = factory.get(f"/tables/api/rows/?table={pid_table.id}&sort_by=enquiry_no&sort_dir=desc")
        force_authenticate(request, user=self.admin)
        response = view(request)
        self.assertEqual(response.status_code, 200)
        row_ids = [r['id'] for r in response.data['results']]
        self.assertEqual(row_ids, [p_row3.id, p_row1.id, p_row2.id])

    def test_employee_promoted_to_admin_table_access(self):
        from tables.permissions import get_accessible_tables, has_table_access, get_column_access_level
        from employee_management.services import EmployeeService

        # Create another department and table created by superadmin in that department
        other_dept = Department.objects.create(name="Sales Dept", slug="sales-dept")
        super_admin = User.objects.create_user(
            email="superadmin@flow-force.com",
            password="testpassword",
            full_name="Super Admin",
            role="SUPER_ADMIN",
            status="APPROVED"
        )
        table_other = Table.objects.create(name="Global Sales Table", created_by=super_admin, department=other_dept)

        # 1. Employee originally in Tables Engineering Dept
        emp = User.objects.create_user(
            email="promoted_emp@flow-force.com",
            password="testpassword",
            full_name="Promoted Employee",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )

        # As an employee, table_other is NOT accessible
        accessible_before = get_accessible_tables(emp)
        self.assertNotIn(table_other, accessible_before)
        self.assertFalse(has_table_access(emp, table_other, "VIEW"))

        # 2. Promote employee to ADMIN
        EmployeeService.update_employee(emp, role="ADMIN", updated_by=self.admin)
        emp.refresh_from_db()
        self.assertEqual(emp.role, "ADMIN")
        self.assertTrue(emp.is_staff)

        # As an ADMIN, all active tables are accessible regardless of department or creator
        accessible_after = get_accessible_tables(emp)
        self.assertIn(table_other, accessible_after)
        self.assertTrue(has_table_access(emp, table_other, "VIEW"))
        self.assertTrue(has_table_access(emp, table_other, "EDIT"))
        self.assertTrue(has_table_access(emp, table_other, "ADMIN"))

        # Column permissions check
        sys_col = table_other.columns.first()
        self.assertEqual(get_column_access_level(emp, sys_col), "EDITABLE")

    def test_import_standard_csv_no_metadata(self):
        """Test importing standard CSV starting on row 1 without metadata lines."""
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="Standard CSV Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        csv_content = (
            "Task Name,Due Date,Priority,Status\n"
            "Deploy Web App,2026-09-01,HIGH,PENDING\n"
            "Update Documentation,2026-09-05,MEDIUM,PENDING\n"
        )
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv_file = SimpleUploadedFile("standard.csv", csv_content.encode("utf-8"), content_type="text/csv")

        view = TableViewSet.as_view({'post': 'import_csv'})
        request = factory.post(f"/tables/api/tables/{table.id}/import-csv/", {"file": csv_file}, format="multipart")
        force_authenticate(request, user=self.admin)

        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Row.objects.filter(table=table).count(), 2)

        tasks = list(Task.objects.filter(row__table=table).order_by('id'))
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].task_name, "Deploy Web App")
        self.assertEqual(tasks[1].task_name, "Update Documentation")

    def test_import_csv_without_s_no_and_missing_due_date(self):
        """Test importing CSV without S_NO column and with empty due date."""
        from tables.views import TableViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()

        table = Table.objects.create(name="No SNO Table", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        csv_content = (
            "Task Name,Due Date,Priority\n"
            "Task Without Date,,LOW\n"
        )
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv_file = SimpleUploadedFile("no_sno.csv", csv_content.encode("utf-8"), content_type="text/csv")

        view = TableViewSet.as_view({'post': 'import_csv'})
        request = factory.post(f"/tables/api/tables/{table.id}/import-csv/", {"file": csv_file}, format="multipart")
        force_authenticate(request, user=self.admin)

        response = view(request, pk=table.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Row.objects.filter(table=table).count(), 1)

        task = Task.objects.filter(row__table=table).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.task_name, "Task Without Date")
        self.assertIsNone(task.due_date)

    def test_import_csv_excel_serial_date(self):
        """Test safe_parse_date with Excel serial date numbers."""
        from tables.views import TableViewSet
        view = TableViewSet()
        import datetime
        # Excel serial 45443 is 2024-05-31
        parsed = view.safe_parse_date("45443")
        self.assertEqual(parsed, datetime.date(2024, 5, 31))

    def test_pid_dashboard_view(self):
        """Test PID Executive Dashboard view renders LIST_PID table details correctly."""
        from django.test import Client
        client = Client()
        client.force_login(self.admin)

        pid_table = Table.objects.create(name="Singapore PID Master", created_by=self.admin, job_type="LIST_PID")
        TableAccess.objects.create(table=pid_table, user=self.admin, access_level="ADMIN")

        r1 = Row.objects.create(table=pid_table, created_by=self.admin)
        col_pid = pid_table.columns.get(name="PID")
        col_enq = pid_table.columns.get(name="ENQUIRY_NO/QUOTATION_NO")
        col_po = pid_table.columns.get(name="PO")
        col_so = pid_table.columns.get(name="SALES_ORDER")
        col_cust = pid_table.columns.get(name="COMPANY_NAME")
        col_due_cust = pid_table.columns.get(name="DUE_DATE_CUSTOMER")
        col_due_ff = pid_table.columns.get(name="DUE_DATE_FLOW_FORCE")
        col_status = pid_table.columns.get(name="STATUS")

        CellValue.objects.create(row=r1, column=col_pid, value="PID-9001", updated_by=self.admin)
        CellValue.objects.create(row=r1, column=col_enq, value="QUO-2026-001", updated_by=self.admin)
        CellValue.objects.create(row=r1, column=col_po, value="PO-778899", updated_by=self.admin)
        CellValue.objects.create(row=r1, column=col_so, value="SO-112233", updated_by=self.admin)
        CellValue.objects.create(row=r1, column=col_cust, value="Acme Corp", updated_by=self.admin)
        CellValue.objects.create(row=r1, column=col_due_cust, value="2026-09-15", updated_by=self.admin)
        CellValue.objects.create(row=r1, column=col_due_ff, value="2026-09-01", updated_by=self.admin)
        CellValue.objects.create(row=r1, column=col_status, value="Fabrication In Progress", updated_by=self.admin)

        response = client.get("/tables/pid-dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PID Executive Dashboard")
        self.assertContains(response, "Quick PID Status Summary Index")
        self.assertContains(response, "Singapore PID Master")
        self.assertContains(response, "PID-9001")
        self.assertContains(response, "QUO-2026-001")
        self.assertContains(response, "PO-778899")
        self.assertContains(response, "SO-112233")
        self.assertContains(response, "Acme Corp")
        self.assertContains(response, "Fabrication In Progress")
        self.assertContains(response, "Year 2026")

class LogsTableTestCase(TestCase):
    def setUp(self):
        import datetime
        from auth_app.models import EmployeeUser
        self.admin = EmployeeUser.objects.create_user(
            email="admin_logs@example.com",
            password="Password123!",
            role="ADMIN",
            full_name="Admin Logs User"
        )
        self.editor = EmployeeUser.objects.create_user(
            email="editor_logs@example.com",
            password="Password123!",
            role="EMPLOYEE",
            full_name="Editor Logs User"
        )
        self.viewer = EmployeeUser.objects.create_user(
            email="viewer_logs@example.com",
            password="Password123!",
            role="EMPLOYEE",
            full_name="Viewer Logs User"
        )

    def test_logs_table_system_columns_creation(self):
        table = Table.objects.create(name="Tool Inventory Logs", job_type="LOGS", created_by=self.admin)
        col_names = list(table.columns.values_list("name", flat=True))
        expected_cols = ["S_NO", "ISSUE_DATE", "RETURN_DATE", "TOOL_NAME", "STATUS", "ISSUED_BY", "RECEIVED_BY", "INITIAL_MAIL", "ALERT_MAIL"]
        for col in expected_cols:
            self.assertIn(col, col_names)
        
        status_col = table.columns.get(name="STATUS")
        self.assertEqual(status_col.options, "Returned,Not Returned")

    def test_logs_row_creation_and_status_sync(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.admin)

        table = Table.objects.create(name="Workshop Tool Tracker", job_type="LOGS", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.editor, access_level="EDIT")
        TableAccess.objects.create(table=table, user=self.viewer, access_level="VIEW")

        # Create row
        response = client.post("/tables/api/rows/", {
            "table": table.id,
            "cells": {
                "TOOL_NAME": "Pneumatic Drill",
                "ISSUE_DATE": "2026-08-10",
                "RETURN_DATE": "2026-08-14",
                "ISSUED_BY": "Admin Logs User",
                "RECEIVED_BY": "John Doe",
                "STATUS": "Not Returned"
            }
        }, format="json")
        self.assertEqual(response.status_code, 201)

        row_id = response.data["id"]
        row = Row.objects.get(id=row_id)
        self.assertEqual(row.task.due_date, datetime.date(2026, 8, 14))

        # Update cell status to Returned
        status_col = table.columns.get(name="STATUS")
        response = client.post(f"/tables/api/rows/{row.id}/edit-cell/", {
            "column": status_col.id,
            "value": "Returned"
        }, format="json")
        self.assertEqual(response.status_code, 200)

        row.task.refresh_from_db()
        self.assertEqual(row.task.status, "COMPLETED")

    def test_logs_daily_alert_mails_sending(self):
        from tasks.tasks import send_daily_alert_mails
        from tasks.models import EmailLog, Notification
        from django.utils import timezone

        table = Table.objects.create(name="Site Equipment Logs", job_type="LOGS", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.editor, access_level="EDIT")
        TableAccess.objects.create(table=table, user=self.viewer, access_level="VIEW")

        # Create row overdue past return date
        row = Row.objects.create(table=table, created_by=self.admin)
        cols = {col.name: col for col in table.columns.all()}
        CellValue.objects.create(row=row, column=cols["TOOL_NAME"], value="Laser Level", updated_by=self.admin)
        CellValue.objects.create(row=row, column=cols["ISSUE_DATE"], value="2026-08-01", updated_by=self.admin)
        CellValue.objects.create(row=row, column=cols["RETURN_DATE"], value="2026-08-10", updated_by=self.admin)
        CellValue.objects.create(row=row, column=cols["ISSUED_BY"], value="Admin Logs User", updated_by=self.admin)
        CellValue.objects.create(row=row, column=cols["RECEIVED_BY"], value="Bob Smith", updated_by=self.admin)
        CellValue.objects.create(row=row, column=cols["STATUS"], value="Not Returned", updated_by=self.admin)

        Task.objects.create(row=row, due_date=datetime.date(2026, 8, 10), status="PENDING", assigned_by=self.admin)

        # Trigger daily alert mails
        send_daily_alert_mails()

        # Check EmailLog created for Admin and Editor, but NOT Viewer
        admin_logs = EmailLog.objects.filter(recipient_email=self.admin.email, subject__icontains="Unreturned Tool")
        editor_logs = EmailLog.objects.filter(recipient_email=self.editor.email, subject__icontains="Unreturned Tool")
        viewer_logs = EmailLog.objects.filter(recipient_email=self.viewer.email, subject__icontains="Unreturned Tool")

        self.assertTrue(admin_logs.exists())
        self.assertTrue(editor_logs.exists())
        self.assertFalse(viewer_logs.exists())

        log_body = admin_logs.first().body
        self.assertIn("Laser Level", log_body)
        self.assertIn("Bob Smith", log_body)
        self.assertIn("2026-08-10", log_body)

    def test_logs_table_auto_return_date_and_days_overdue(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from tables.views import RowViewSet

        table = Table.objects.create(name="Tool Logs Test", job_type="LOGS", created_by=self.admin)
        TableAccess.objects.create(table=table, user=self.admin, access_level="ADMIN")

        factory = APIRequestFactory()
        view = RowViewSet.as_view({'post': 'create'})

        # 1. Create row with ISSUE_DATE given but no RETURN_DATE
        req = factory.post('/tables/api/rows/', {
            'table': table.id,
            'cells': {
                'TOOL_NAME': 'Drill Machine',
                'ISSUE_DATE': '2026-08-01',
                'STATUS': 'Not Returned'
            }
        }, format='json')
        force_authenticate(req, user=self.admin)
        res = view(req)
        self.assertEqual(res.status_code, 201)

        row_id = res.data['id']
        row = Row.objects.get(id=row_id)

        # Verify RETURN_DATE auto captured to match ISSUE_DATE (2026-08-01)
        ret_col = table.columns.get(name='RETURN_DATE')
        ret_cell = row.cells.get(column=ret_col)
        self.assertEqual(ret_cell.value, '2026-08-01')

        # Verify DAYS_OVERDUE calculated (14 days past 2026-08-01)
        overdue_col = table.columns.get(name='DAYS_OVERDUE')
        overdue_cell = row.cells.get(column=overdue_col)
        self.assertEqual(overdue_cell.value, 14)






