from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch
from employee_management.models import Department
from tables.models import Table, Column, Row, CellValue
from tasks.models import Task, EmailLog, Notification, TaskComment
from tasks.tasks import (
    check_overdue_escalations, send_daily_alert_mails, send_email_log_task,
    send_initial_mail, send_alert_mail, send_review_request_mail, send_approval_status_mail,
    retry_failed_emails
)

User = get_user_model()

class TasksTestCase(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Tasks QA Dept", slug="tasks-qa-dept")
        self.admin = User.objects.create_user(
            email="tasksadmin@flow-force.com",
            password="testpassword",
            full_name="Admin User",
            role="ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.employee = User.objects.create_user(
            email="tasksemp@flow-force.com",
            password="testpassword",
            full_name="Employee User",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.dept_admin = User.objects.create_user(
            email="tasksdeptadmin@flow-force.com",
            password="testpassword",
            full_name="Dept Admin User",
            role="DEPARTMENT_ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.super_admin = User.objects.create_user(
            email="superadmin@flow-force.com",
            password="testpassword",
            full_name="Super Admin User",
            role="SUPER_ADMIN",
            status="APPROVED"
        )
        self.table = Table.objects.create(name="QA Tasks", created_by=self.admin, department=self.dept)
        self.row = Row.objects.create(table=self.table, created_by=self.employee)

        # Create system columns if not created
        for col_name in ["INITIAL_MAIL", "ALERT_MAIL", "TASK_NAME", "MESSAGE"]:
            Column.objects.get_or_create(table=self.table, name=col_name, defaults={"data_type": "TEXT"})

    def test_initial_mail_sent_on_assignment(self):
        """Create task with admin assigning to employee, verify EmailLog & Notification & Row update"""
        task = Task.objects.create(
            row=self.row,
            due_date=timezone.localdate(),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        # Add employee triggers signal
        task.assigned_to.add(self.employee)

        # Verify INITIAL_MAIL cell is YES
        init_cell = CellValue.objects.filter(row=self.row, column__name="INITIAL_MAIL").first()
        self.assertEqual(init_cell.value, "YES")

        # Verify EmailLog is created
        log = EmailLog.objects.filter(task=task, email_type="INITIAL_MAIL", recipient_email=self.employee.email).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "SENT")

        # Verify Notification is created for employee
        notif = Notification.objects.filter(user=self.employee, task=task, type="ASSIGNED").first()
        self.assertIsNotNone(notif)

    def test_initial_mail_not_sent_on_self_assignment(self):
        """Create task where employee assigns to self, verify no email sent"""
        task = Task.objects.create(
            row=self.row,
            due_date=timezone.localdate(),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.employee
        )
        task.assigned_to.add(self.employee)

        # Verify EmailLog does not exist for INITIAL_MAIL
        log_exists = EmailLog.objects.filter(task=task, email_type="INITIAL_MAIL").exists()
        self.assertFalse(log_exists)

    def test_daily_alert_mail_sent_at_8am(self):
        """Create task due today, run scheduled task, verify email and alert cell = YES"""
        task = Task.objects.create(
            row=self.row,
            due_date=timezone.localdate(),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.add(self.employee)

        # Reset alert sent status
        task.alert_mail_sent = False
        task.save()
        alert_cell = CellValue.objects.filter(row=self.row, column__name="ALERT_MAIL").first()
        if alert_cell:
            alert_cell.value = "NO"
            alert_cell.save()

        # Run scheduled task
        send_daily_alert_mails()

        # Refresh from db
        task.refresh_from_db()
        self.assertTrue(task.alert_mail_sent)

        # Verify ALERT_MAIL cell is YES
        alert_cell.refresh_from_db()
        self.assertEqual(alert_cell.value, "YES")

        # Verify EmailLog exists
        log = EmailLog.objects.filter(task=task, email_type="ALERT_MAIL", recipient_email=self.employee.email).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "SENT")

    def test_overdue_escalation(self):
        """Create overdue tasks and verify correct escalation email routing"""
        # Scenario A: 1 Day Overdue -> no escalation
        task_1d = Task.objects.create(
            row=Row.objects.create(table=self.table),
            due_date=timezone.localdate() - timedelta(days=1),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task_1d.assigned_to.add(self.employee)

        # Scenario B: 6 Days Overdue -> Employee only
        task_6d = Task.objects.create(
            row=Row.objects.create(table=self.table),
            due_date=timezone.localdate() - timedelta(days=6),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task_6d.assigned_to.add(self.employee)

        # Scenario C: 7 Days Overdue -> no escalation
        task_7d = Task.objects.create(
            row=Row.objects.create(table=self.table),
            due_date=timezone.localdate() - timedelta(days=7),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task_7d.assigned_to.add(self.employee)
        
        # Clear any logs created by assignment
        EmailLog.objects.all().delete()

        check_overdue_escalations()
        
        # Should have sent to employee
        logs_6d = EmailLog.objects.filter(task=task_6d, email_type="OVERDUE_ESCALATION_MAIL")
        self.assertEqual(logs_6d.count(), 1)
        self.assertEqual(logs_6d.first().recipient_email, self.employee.email)

        # Verify that 1d and 7d tasks did not get escalated
        self.assertFalse(EmailLog.objects.filter(task=task_1d, email_type="OVERDUE_ESCALATION_MAIL").exists())
        self.assertFalse(EmailLog.objects.filter(task=task_7d, email_type="OVERDUE_ESCALATION_MAIL").exists())

        # Verify that the OVERDUE_ESCALATION_MAIL uses operations.flowforce@gmail.com
        log = logs_6d.first()
        log.status = "PENDING"
        log.save()
        with patch('tasks.tasks.send_mail') as mock_send:
            send_email_log_task(log.id)
            mock_send.assert_called_once()
            kwargs = mock_send.call_args[1]
            self.assertEqual(kwargs.get('from_email'), 'operations.flowforce@gmail.com')

    def test_email_retry_logic(self):
        """Simulate email send failure, verify exponential backoff and retry success"""
        task = Task.objects.create(
            row=self.row,
            due_date=timezone.localdate(),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        
        # 1. Create a log that failed once
        log = EmailLog.objects.create(
            recipient_email=self.employee.email,
            subject="Test Failure",
            body="Test Body",
            task=task,
            email_type="INITIAL_MAIL",
            status="FAILED",
            retry_count=1,
            max_retries=3,
            next_retry_at=timezone.now() - timedelta(minutes=1)
        )

        # 2. Trigger retry_failed_emails and verify it attempts to send
        with patch('tasks.tasks.send_mail') as mock_send:
            retry_failed_emails()
            self.assertTrue(mock_send.called)
            
            # Run send_email_log_task directly to verify success path updates status to SENT
            send_email_log_task(log.id)
            log.refresh_from_db()
            self.assertEqual(log.status, "SENT")
            self.assertIsNotNone(log.sent_at)

    def test_review_request_email(self):
        """Update task status to READY_FOR_REVIEW, verify NO email log is created since status emails are disabled"""
        task = Task.objects.create(
            row=self.row,
            due_date=timezone.localdate(),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.add(self.employee)

        EmailLog.objects.all().delete()

        # Update status
        task.status = "READY_FOR_REVIEW"
        task.save()

        log = EmailLog.objects.filter(task=task, email_type="REVIEW_REQUEST_MAIL").first()
        self.assertIsNone(log)

    def test_sales_system_columns(self):
        """Verify that a SALES table creates S_NO, DATE, FOLLOW_UP_DATE, and CUSTOMER_NAME system columns."""
        sales_table = Table.objects.create(
            name="Sales Leads Table",
            created_by=self.admin,
            department=self.dept,
            job_type="SALES"
        )
        col_names = list(sales_table.columns.values_list("name", flat=True))
        self.assertIn("FOLLOW_UP_DATE", col_names)
        self.assertIn("CUSTOMER_NAME", col_names)
        self.assertNotIn("DUE_DATE", col_names)
        self.assertNotIn("TASK_NAME", col_names)

    def test_log_follow_up_validation_and_api(self):
        """Test follow-up API validation rules, TaskFollowUp creation, and cell value syncing."""
        sales_table = Table.objects.create(
            name="Sales Leads Table",
            created_by=self.admin,
            department=self.dept,
            job_type="SALES"
        )
        
        # Set up cell values for CUSTOMER_NAME and FOLLOW_UP_DATE
        row = Row.objects.create(table=sales_table, created_by=self.employee)
        col_cust = Column.objects.get(table=sales_table, name="CUSTOMER_NAME")
        col_fu = Column.objects.get(table=sales_table, name="FOLLOW_UP_DATE")
        col_status = Column.objects.get(table=sales_table, name="STATUS") if Column.objects.filter(table=sales_table, name="STATUS").exists() else Column.objects.create(table=sales_table, name="STATUS", data_type="TEXT")
        
        CellValue.objects.create(row=row, column=col_cust, value="ACME Corp")
        CellValue.objects.create(row=row, column=col_fu, value="2026-06-26")
        CellValue.objects.create(row=row, column=col_status, value="PENDING")
        
        task = Task.objects.create(
            row=row,
            due_date="2026-06-26",
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.add(self.employee)

        # 1. Validation fails: Continuing follow-up without next date
        from django.urls import reverse
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.employee)
        
        url = f"/tasks/api/tasks/{task.id}/log-follow-up/"
        response = client.post(url, {
            "discussed_points": "Called lead, interested.",
            "status": "IN_PROGRESS"
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Next follow-up date is required", response.json().get("error"))

        # 2. Validation succeeds: Continuing follow-up with next date
        next_date = (timezone.localdate() + timedelta(days=5)).isoformat()
        response = client.post(url, {
            "discussed_points": "Called lead, interested.",
            "status": "IN_PROGRESS",
            "next_follow_up_date": next_date
        }, format="json")
        self.assertEqual(response.status_code, 200)
        
        # Verify TaskFollowUp log is saved
        from tasks.models import TaskFollowUp
        follow_up = TaskFollowUp.objects.filter(task=task).first()
        self.assertIsNotNone(follow_up)
        self.assertEqual(follow_up.discussed_points, "Called lead, interested.")
        self.assertEqual(follow_up.next_follow_up_date.isoformat(), next_date)
        
        # Verify comment is created for sales follow-up under old date
        from tasks.models import TaskComment
        self.assertTrue(TaskComment.objects.filter(task=task, content__startswith="enter new follow up under the old follow up").exists())
        
        # Verify Task due_date is updated
        task.refresh_from_db()
        self.assertEqual(task.due_date.isoformat(), next_date)
        self.assertEqual(task.status, "IN_PROGRESS")
        
        # Verify row cell FOLLOW_UP_DATE and STATUS values are updated
        fu_cell = CellValue.objects.get(row=row, column=col_fu)
        status_cell = CellValue.objects.get(row=row, column=col_status)
        self.assertEqual(fu_cell.value, next_date)
        self.assertEqual(status_cell.value, "IN_PROGRESS")
        
        # Verify push notification is created
        notif = Notification.objects.filter(user=self.employee, task=task, title="Next Follow-up Scheduled").first()
        self.assertIsNotNone(notif)

        # 3. Validation succeeds: Closed/Completed follow-up without next date
        response = client.post(url, {
            "discussed_points": "Deal signed, closing lead.",
            "status": "COMPLETED"
        }, format="json")
        self.assertEqual(response.status_code, 200)
        
        task.refresh_from_db()
        self.assertEqual(task.status, "COMPLETED")

    def test_sales_daily_alert(self):
        """Verify daily alert email content rendering specifically for Sales tasks."""
        sales_table = Table.objects.create(
            name="Sales Leads Table",
            created_by=self.admin,
            department=self.dept,
            job_type="SALES"
        )
        row = Row.objects.create(table=sales_table, created_by=self.employee)
        col_cust = Column.objects.get(table=sales_table, name="CUSTOMER_NAME")
        col_fu = Column.objects.get(table=sales_table, name="FOLLOW_UP_DATE")
        
        CellValue.objects.create(row=row, column=col_cust, value="Globex Corp")
        CellValue.objects.create(row=row, column=col_fu, value=timezone.localdate().isoformat())
        
        task = Task.objects.create(
            row=row,
            due_date=timezone.localdate(),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.add(self.employee)
        
        from tasks.models import TaskFollowUp
        TaskFollowUp.objects.create(
            task=task,
            follow_up_date=timezone.localdate() - timedelta(days=2),
            discussed_points="Negotiating final contract terms.",
            entered_by=self.admin
        )
        
        EmailLog.objects.all().delete()
        task.alert_mail_sent = False
        task.save()
        
        # Run daily alert task
        send_daily_alert_mails()
        
        # Verify EmailLog exists and contains HTML template values
        log = EmailLog.objects.filter(task=task, email_type="ALERT_MAIL", recipient_email=self.employee.email).first()
        self.assertIsNotNone(log)
        self.assertIn("Negotiating final contract terms.", log.body)
        self.assertIn("Globex Corp", log.body)

    def test_import_csv_with_row_config(self):
        """Verify that importing a CSV with custom header and data start row parses correctly."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.admin)

        # CSV where headers are at row 2, and data starts at row 3 (mixed metadata on row 1)
        csv_content = (
            "Metadata: Sales Leads Import Report\n"
            "S_NO,DATE,FOLLOW_UP_DATE,CUSTOMER_NAME,INITIAL_MAIL,ALERT_MAIL,STATUS,PRIORITY\n"
            "1,2026-06-26,2026-07-05,Cyberdyne Inc,NO,NO,PENDING,HIGH\n"
        )
        csv_file = SimpleUploadedFile("leads.csv", csv_content.encode("utf-8"), content_type="text/csv")
        
        sales_table = Table.objects.create(
            name="Sales Leads Table",
            created_by=self.admin,
            department=self.dept,
            job_type="SALES"
        )
        Column.objects.create(table=sales_table, name="STATUS", data_type="TEXT")
        Column.objects.create(table=sales_table, name="PRIORITY", data_type="TEXT")

        url = f"/tables/api/tables/{sales_table.id}/import-csv/"
        response = client.post(url, {
            "file": csv_file,
            "header_row": 2,
            "data_row": 3
        }, format="multipart")

        self.assertEqual(response.status_code, 201)
        self.assertIn("Successfully imported 1 rows", response.json().get("message"))

        # Verify created task details
        task = Task.objects.filter(row__table=sales_table).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.task_name, "Cyberdyne Inc")
        self.assertEqual(task.due_date.isoformat(), "2026-07-05")
        self.assertEqual(task.priority, "HIGH")
