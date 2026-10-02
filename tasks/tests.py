from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch
from employee_management.models import Department
from tables.models import Table, Column, Row, CellValue, TableAccess
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

        # Scenario C: 7 Days Overdue (already escalated) -> no re-escalation
        task_7d = Task.objects.create(
            row=Row.objects.create(table=self.table),
            due_date=timezone.localdate() - timedelta(days=7),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin,
            last_escalation_level=6
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


class TableAccessTaskAssignmentRegressionTestCase(TestCase):
    """
    Regression tests proving that TableAccess modifications (create, update, delete)
    NEVER overwrite or alter existing individual task.assigned_to relationships.
    """
    def setUp(self):
        self.dept = Department.objects.create(name="Access QA Dept", slug="access-qa-dept")
        self.admin = User.objects.create_user(
            email="admin_access@flow-force.com",
            password="testpassword",
            full_name="Access Admin",
            role="ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.employee1 = User.objects.create_user(
            email="emp1_access@flow-force.com",
            password="testpassword",
            full_name="Employee One",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.employee2 = User.objects.create_user(
            email="emp2_access@flow-force.com",
            password="testpassword",
            full_name="Employee Two",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.viewer = User.objects.create_user(
            email="viewer_access@flow-force.com",
            password="testpassword",
            full_name="Viewer User",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.table = Table.objects.create(name="Table Access QA", created_by=self.admin, department=self.dept)
        self.row1 = Row.objects.create(table=self.table, created_by=self.admin)
        self.task1 = Task.objects.create(
            row=self.row1,
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        self.task1.assigned_to.set([self.employee1])

    @patch("tasks.tasks.send_initial_mail.delay")
    def test_existing_task_assignees_unchanged_when_table_access_created(self, mock_mail):
        """Proving existing task assignees remain unchanged when TableAccess is created."""
        initial_assignees = list(self.task1.assigned_to.all())
        self.assertEqual(initial_assignees, [self.employee1])

        # Create TableAccess for another user
        access = TableAccess.objects.create(
            table=self.table,
            user=self.viewer,
            access_level="VIEW"
        )

        self.task1.refresh_from_db()
        current_assignees = list(self.task1.assigned_to.all())
        self.assertEqual(current_assignees, [self.employee1])
        self.assertEqual(current_assignees, initial_assignees)

    @patch("tasks.tasks.send_initial_mail.delay")
    def test_existing_task_assignees_unchanged_when_table_access_modified(self, mock_mail):
        """Proving existing task assignees remain unchanged when TableAccess is modified."""
        access = TableAccess.objects.create(
            table=self.table,
            user=self.viewer,
            access_level="VIEW"
        )
        self.task1.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])

        # Modify TableAccess access_level
        access.access_level = "EDIT"
        access.save()

        self.task1.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])

        # Upgrade to ADMIN
        access.access_level = "ADMIN"
        access.save()

        self.task1.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])

    @patch("tasks.tasks.send_initial_mail.delay")
    def test_existing_task_assignees_unchanged_when_table_access_deleted(self, mock_mail):
        """Proving existing task assignees remain unchanged when TableAccess is deleted."""
        access = TableAccess.objects.create(
            table=self.table,
            user=self.viewer,
            access_level="EDIT"
        )
        self.task1.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])

        # Delete TableAccess
        access.delete()

        self.task1.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])

    @patch("tasks.tasks.send_initial_mail.delay")
    def test_multiple_tasks_with_distinct_assignees_preserved(self, mock_mail):
        """Proving distinct task assignees across multiple rows are all preserved through TableAccess lifecycle."""
        row2 = Row.objects.create(table=self.table, created_by=self.admin)
        task2 = Task.objects.create(
            row=row2,
            priority="LOW",
            status="PENDING",
            assigned_by=self.admin
        )
        task2.assigned_to.set([self.employee2])

        # Create TableAccess with department
        dept_access = TableAccess.objects.create(
            table=self.table,
            department=self.dept,
            access_level="EDIT"
        )

        self.task1.refresh_from_db()
        task2.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])
        self.assertEqual(list(task2.assigned_to.all()), [self.employee2])

        # Modify department access
        dept_access.access_level = "VIEW"
        dept_access.save()

        self.task1.refresh_from_db()
        task2.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])
        self.assertEqual(list(task2.assigned_to.all()), [self.employee2])

        # Delete department access
        dept_access.delete()

        self.task1.refresh_from_db()
        task2.refresh_from_db()
        self.assertEqual(list(self.task1.assigned_to.all()), [self.employee1])
        self.assertEqual(list(task2.assigned_to.all()), [self.employee2])


class OverdueEscalationMissedDayRegressionTestCase(TestCase):
    """
    BUG-03 Regression tests for check_overdue_escalations handling missed-day runs.
    """
    def setUp(self):
        self.dept = Department.objects.create(name="Escalation QA Dept", slug="esc-qa-dept")
        self.admin = User.objects.create_user(
            email="escadmin@flow-force.com",
            password="testpassword",
            full_name="Esc Admin",
            role="ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.employee = User.objects.create_user(
            email="escemp@flow-force.com",
            password="testpassword",
            full_name="Esc Employee",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.table = Table.objects.create(name="Standard Table", created_by=self.admin, department=self.dept, job_type="STANDARD")
        self.list_pid_table = Table.objects.create(name="PID Table", created_by=self.admin, department=self.dept, job_type="LIST_PID")

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_escalation_triggers_on_exact_threshold_day(self, mock_email_task):
        """Task 6 days overdue escalates to level 6 on exact threshold day."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today - timedelta(days=6),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin,
            last_escalation_level=0
        )
        task.assigned_to.set([self.employee])

        mock_email_task.reset_mock()
        check_overdue_escalations()

        task.refresh_from_db()
        self.assertEqual(task.last_escalation_level, 6)
        self.assertIsNotNone(task.last_escalation_at)
        self.assertTrue(mock_email_task.called)

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_escalation_triggers_on_threshold_plus_one_day(self, mock_email_task):
        """Task 7 days overdue (missed day 6) escalates to level 6."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today - timedelta(days=7),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin,
            last_escalation_level=0
        )
        task.assigned_to.set([self.employee])

        mock_email_task.reset_mock()
        check_overdue_escalations()

        task.refresh_from_db()
        self.assertEqual(task.last_escalation_level, 6)
        self.assertIsNotNone(task.last_escalation_at)
        self.assertTrue(mock_email_task.called)

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_escalation_triggers_on_threshold_plus_multiple_days(self, mock_email_task):
        """Task 10 days overdue (missed multiple runs) escalates to level 6."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today - timedelta(days=10),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin,
            last_escalation_level=0
        )
        task.assigned_to.set([self.employee])

        mock_email_task.reset_mock()
        check_overdue_escalations()

        task.refresh_from_db()
        self.assertEqual(task.last_escalation_level, 6)
        self.assertTrue(mock_email_task.called)

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_already_escalated_task_is_not_re_escalated(self, mock_email_task):
        """Task with last_escalation_level=6 is not re-escalated on subsequent days."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today - timedelta(days=8),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin,
            last_escalation_level=6
        )
        task.assigned_to.set([self.employee])

        mock_email_task.reset_mock()
        check_overdue_escalations()

        task.refresh_from_db()
        self.assertEqual(task.last_escalation_level, 6)
        self.assertFalse(mock_email_task.called)

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_list_pid_missed_day_one_escalates_on_day_two(self, mock_email_task):
        """LIST_PID task 2 days overdue (missed day 1) escalates to level 1."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.list_pid_table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today - timedelta(days=2),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin,
            last_escalation_level=0
        )
        task.assigned_to.set([self.employee])

        mock_email_task.reset_mock()
        check_overdue_escalations()

        task.refresh_from_db()
        self.assertEqual(task.last_escalation_level, 1)
        self.assertTrue(mock_email_task.called)


class DailyAlertDueTodayRegressionTestCase(TestCase):
    """
    BUG-04 Regression tests for send_daily_alert_mails:
    Verifies that 'Due Today' strictly alerts tasks where due_date == today,
    and excludes overdue or future tasks.
    """
    def setUp(self):
        self.dept = Department.objects.create(name="Daily Alert QA Dept", slug="alert-qa-dept")
        self.admin = User.objects.create_user(
            email="alertadmin@flow-force.com",
            password="testpassword",
            full_name="Alert Admin",
            role="ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.employee = User.objects.create_user(
            email="alertemp@flow-force.com",
            password="testpassword",
            full_name="Alert Employee",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.table = Table.objects.create(name="Alert Table", created_by=self.admin, department=self.dept, job_type="STANDARD")

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_task_due_today_is_included(self, mock_email_task):
        """Task with due_date == today is alerted."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today,
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.set([self.employee])

        send_daily_alert_mails()

        task.refresh_from_db()
        self.assertTrue(task.alert_mail_sent)
        self.assertTrue(EmailLog.objects.filter(recipient_email=self.employee.email, email_type="ALERT_MAIL").exists())

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_task_overdue_yesterday_is_excluded(self, mock_email_task):
        """Task overdue yesterday (due_date == today - 1) is NOT included in Due Today alert."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today - timedelta(days=1),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.set([self.employee])

        send_daily_alert_mails()

        task.refresh_from_db()
        self.assertFalse(task.alert_mail_sent)
        self.assertFalse(EmailLog.objects.filter(recipient_email=self.employee.email, email_type="ALERT_MAIL").exists())

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_task_overdue_30_days_is_excluded(self, mock_email_task):
        """Task overdue 30 days ago is NOT included in Due Today alert."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today - timedelta(days=30),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.set([self.employee])

        send_daily_alert_mails()

        task.refresh_from_db()
        self.assertFalse(task.alert_mail_sent)
        self.assertFalse(EmailLog.objects.filter(recipient_email=self.employee.email, email_type="ALERT_MAIL").exists())

    @patch("tasks.tasks.send_email_log_task.delay")
    def test_future_task_is_excluded(self, mock_email_task):
        """Task due in the future (due_date == today + 5) is NOT included in Due Today alert."""
        today = timezone.localdate()
        row = Row.objects.create(table=self.table, created_by=self.admin)
        task = Task.objects.create(
            row=row,
            due_date=today + timedelta(days=5),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task.assigned_to.set([self.employee])

        send_daily_alert_mails()

        task.refresh_from_db()
        self.assertFalse(task.alert_mail_sent)
        self.assertFalse(EmailLog.objects.filter(recipient_email=self.employee.email, email_type="ALERT_MAIL").exists())


class GlobalContextNotificationOptimizationRegressionTestCase(TestCase):
    """
    Regression test suite for Phase 2A: Notification & Global Context Performance Optimization.
    Validates query count reduction and behavioral fidelity of tasks/context_processors.py.
    """

    def setUp(self):
        from django.test import RequestFactory
        from tasks.context_processors import global_context
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        self.factory = RequestFactory()
        self.global_context = global_context
        self.CaptureQueriesContext = CaptureQueriesContext
        self.connection = connection

        self.dept = Department.objects.create(name="Notification Perf Dept", slug="notif-perf-dept")
        self.user = User.objects.create_user(
            email="notif_perf_user@flow-force.com",
            password="testpassword",
            full_name="Notification Perf User",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )

        self.table_gen = Table.objects.create(name="General Project Table", job_type="GENERAL", department=self.dept)
        self.col_gen_task = Column.objects.create(table=self.table_gen, name="TASK_NAME", data_type="TEXT")

        self.table_sales = Table.objects.create(name="Sales Client Table", job_type="SALES", department=self.dept)
        self.col_sales_cust = Column.objects.create(table=self.table_sales, name="CUSTOMER_NAME", data_type="TEXT")

        self.table_pid = Table.objects.create(name="PID Engineering Table", job_type="LIST_PID", department=self.dept)
        self.col_pid_enq = Column.objects.create(table=self.table_pid, name="ENQUIRY_NO/QUOTATION_NO", data_type="TEXT")

    def test_case_a_zero_notifications(self):
        """Case A: Authenticated user with 0 notifications executes <= 3 queries."""
        request = self.factory.get("/")
        request.user = self.user

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            context = self.global_context(request)

        self.assertLessEqual(len(ctx_queries), 3)
        self.assertEqual(context["task_notifications_unread"], 0)
        self.assertEqual(context["unread_notifications"], [])
        self.assertEqual(context["read_notifications"], [])

    def test_case_b_one_notification(self):
        """Case B: Authenticated user with 1 notification executes <= 4 queries and preserves UI contract."""
        row = Row.objects.create(table=self.table_gen, created_by=self.user)
        CellValue.objects.create(row=row, column=self.col_gen_task, value="Urgent Pump Fix")
        task = Task.objects.create(row=row)
        Notification.objects.create(
            user=self.user,
            task=task,
            title="Single Notification",
            description="Fix pump promptly",
            is_read=False
        )

        request = self.factory.get("/")
        request.user = self.user

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            context = self.global_context(request)

        # Baseline was 6 queries; optimized is <= 4
        self.assertLessEqual(len(ctx_queries), 4)
        self.assertEqual(context["task_notifications_unread"], 1)
        self.assertEqual(len(context["unread_notifications"]), 1)
        notif_item = context["unread_notifications"][0]
        self.assertEqual(notif_item["title"], "Single Notification")
        self.assertEqual(notif_item["task"]["table_name"], "General Project Table")
        self.assertEqual(notif_item["task"]["task_name"], "Urgent Pump Fix")

    def test_case_c_fifteen_notifications_no_linear_growth(self):
        """Case C: 15 notifications across mixed table types does NOT grow linearly (<= 5 queries)."""
        tables = [
            (self.table_gen, self.col_gen_task, "Task General"),
            (self.table_sales, self.col_sales_cust, "Acme Industrial"),
            (self.table_pid, self.col_pid_enq, "ENQ-2026-99")
        ]

        for i in range(15):
            tbl, col, base_name = tables[i % len(tables)]
            r = Row.objects.create(table=tbl, created_by=self.user)
            CellValue.objects.create(row=r, column=col, value=f"{base_name} #{i+1}")
            t = Task.objects.create(row=r)
            Notification.objects.create(
                user=self.user,
                task=t,
                title=f"Notification #{i+1}",
                description=f"Description #{i+1}",
                is_read=False
            )

        request = self.factory.get("/")
        request.user = self.user

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            context = self.global_context(request)

        # Before optimization: 48 queries. After optimization: <= 5 queries.
        self.assertLessEqual(len(ctx_queries), 5)
        self.assertEqual(context["task_notifications_unread"], 15)
        self.assertEqual(len(context["unread_notifications"]), 15)

        # Check notification content fidelity across different job types
        for item in context["unread_notifications"]:
            self.assertTrue(item["task"]["table_name"])
            self.assertTrue(item["task"]["task_name"])
            self.assertNotEqual(item["task"]["task_name"], "Unnamed Task")

    def test_case_d_multiple_read_and_unread_notifications(self):
        """Case D: 15 unread + 15 read notifications (30 total) remains bounded at <= 6 queries."""
        for i in range(15):
            r = Row.objects.create(table=self.table_gen, created_by=self.user)
            CellValue.objects.create(row=r, column=self.col_gen_task, value=f"Unread Item {i+1}")
            t = Task.objects.create(row=r)
            Notification.objects.create(
                user=self.user,
                task=t,
                title=f"Unread {i+1}",
                description=f"Desc unread {i+1}",
                is_read=False
            )

        for i in range(15):
            r = Row.objects.create(table=self.table_sales, created_by=self.user)
            CellValue.objects.create(row=r, column=self.col_sales_cust, value=f"Client Lead {i+1}")
            t = Task.objects.create(row=r)
            Notification.objects.create(
                user=self.user,
                task=t,
                title=f"Read {i+1}",
                description=f"Desc read {i+1}",
                is_read=True
            )

        request = self.factory.get("/")
        request.user = self.user

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            context = self.global_context(request)

        # Before optimization: 93 queries. After optimization: <= 6 queries.
        self.assertLessEqual(len(ctx_queries), 6)
        self.assertEqual(context["task_notifications_unread"], 15)
        self.assertEqual(len(context["unread_notifications"]), 15)
        self.assertEqual(len(context["read_notifications"]), 15)

    def test_task_name_without_prefetch_fallback(self):
        """Ensure task.task_name falls back cleanly when prefetch cache is not present."""
        row = Row.objects.create(table=self.table_gen, created_by=self.user)
        CellValue.objects.create(row=row, column=self.col_gen_task, value="Direct Task")
        task = Task.objects.create(row=row)

        # Calling directly on fresh task without prefetched cells
        fresh_task = Task.objects.get(id=task.id)
        self.assertEqual(fresh_task.task_name, "Direct Task")

    def test_anonymous_user_returns_empty_context(self):
        """Unauthenticated requests return empty context without querying notifications."""
        from django.contrib.auth.models import AnonymousUser
        request = self.factory.get("/")
        request.user = AnonymousUser()

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            context = self.global_context(request)

        self.assertEqual(len(ctx_queries), 0)
        self.assertEqual(context, {})


class TasksReportsExportOptimizationRegressionTestCase(TestCase):
    """
    Regression test suite for Phase 2B Reports Export Optimization.
    Validates that CSV, Excel, and PDF exports execute with constant-bound query complexity
    and do not generate N+1 queries regardless of task or assignee count.
    """

    def setUp(self):
        from django.test.client import RequestFactory
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.factory = RequestFactory()
        self.connection = connection
        self.CaptureQueriesContext = CaptureQueriesContext

        self.dept = Department.objects.create(name="Analytics Dept", slug="analytics-dept")
        self.admin = User.objects.create_user(
            email="exportadmin@flow-force.com",
            password="testpassword",
            full_name="Export Admin",
            role="ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.employee1 = User.objects.create_user(
            email="emp1@flow-force.com",
            password="testpassword",
            full_name="Alice Smith",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.employee2 = User.objects.create_user(
            email="emp2@flow-force.com",
            password="testpassword",
            full_name="Bob Jones",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )

        self.table = Table.objects.create(
            name="Operations Log",
            created_by=self.admin,
            department=self.dept
        )

        self.col_task = Column.objects.create(
            table=self.table,
            name="TASK_NAME",
            data_type="TEXT"
        )
        self.col_due = Column.objects.create(
            table=self.table,
            name="DUE_DATE",
            data_type="DATE"
        )

    def _create_sample_tasks(self, count=10):
        from datetime import date, timedelta
        today = date.today()
        tasks = []
        for i in range(count):
            row = Row.objects.create(table=self.table, created_by=self.admin)
            CellValue.objects.create(row=row, column=self.col_task, value=f"Export Task #{i+1}")
            CellValue.objects.create(row=row, column=self.col_due, value=(today + timedelta(days=i)).isoformat())

            status = "COMPLETED" if i % 2 == 0 else "PENDING"
            due_date = today - timedelta(days=2) if i == 1 else today + timedelta(days=i)
            task = Task.objects.create(
                row=row,
                due_date=due_date,
                priority="HIGH" if i % 2 == 0 else "MEDIUM",
                status=status,
                assigned_by=self.admin
            )
            task.assigned_to.set([self.employee1, self.employee2])
            tasks.append(task)
        return tasks

    def test_csv_export_query_count_and_fidelity(self):
        """CSV export executes with constant queries (<= 6) and exports expected columns & values."""
        from tasks.views import reports_view
        self._create_sample_tasks(count=10)

        request = self.factory.get("/tasks/reports/?format=csv")
        request.user = self.admin

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            response = reports_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn('attachment; filename="tasks_report.csv"', response["Content-Disposition"])
        # Query count must remain tightly bounded (no N+1 for tasks, assignees, or rows)
        self.assertLessEqual(len(ctx_queries), 6)

        csv_content = response.content.decode("utf-8")
        self.assertIn("=== TABLE ANALYTICS ===", csv_content)
        self.assertIn("=== EMPLOYEE ANALYTICS ===", csv_content)
        self.assertIn("=== DETAILED TASK REPORT ===", csv_content)
        self.assertIn("S_NO,Task Name,Table Name,Due Date,Priority,Status,Assigned To,Assigned By,Department", csv_content)
        self.assertIn("Export Task #1", csv_content)
        self.assertIn("Operations Log", csv_content)
        self.assertIn("Analytics Dept", csv_content)
        self.assertIn("Alice Smith, Bob Jones", csv_content)

    def test_excel_export_query_count_and_fidelity(self):
        """Excel export executes with constant queries (<= 6) and returns valid xlsx format."""
        from tasks.views import reports_view
        self._create_sample_tasks(count=10)

        request = self.factory.get("/tasks/reports/?format=excel")
        request.user = self.admin

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            response = reports_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.assertIn('attachment; filename="tasks_report.xlsx"', response["Content-Disposition"])
        self.assertLessEqual(len(ctx_queries), 6)
        self.assertGreater(len(response.content), 1000)

    def test_pdf_export_query_count_and_fidelity(self):
        """PDF export executes with constant queries (<= 6) and returns valid PDF document."""
        from tasks.views import reports_view
        self._create_sample_tasks(count=10)

        request = self.factory.get("/tasks/reports/?format=pdf")
        request.user = self.admin

        with self.CaptureQueriesContext(self.connection) as ctx_queries:
            response = reports_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn('attachment; filename="tasks_report.pdf"', response["Content-Disposition"])
        self.assertLessEqual(len(ctx_queries), 6)
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_export_query_count_does_not_grow_linearly(self):
        """Proves query count is O(1) regardless of whether exporting 5 tasks or 25 tasks."""
        from tasks.views import reports_view

        # Baseline: 5 tasks
        self._create_sample_tasks(count=5)
        request5 = self.factory.get("/tasks/reports/?format=csv")
        request5.user = self.admin
        with self.CaptureQueriesContext(self.connection) as ctx5:
            res5 = reports_view(request5)
        self.assertEqual(res5.status_code, 200)
        query_count_5 = len(ctx5)

        # Scale up: Add 20 more tasks (total 25 tasks)
        self._create_sample_tasks(count=20)
        request25 = self.factory.get("/tasks/reports/?format=csv")
        request25.user = self.admin
        with self.CaptureQueriesContext(self.connection) as ctx25:
            res25 = reports_view(request25)
        self.assertEqual(res25.status_code, 200)
        query_count_25 = len(ctx25)

        # Query counts must be identical, proving zero linear N+1 query growth
        self.assertEqual(query_count_5, query_count_25)
        self.assertLessEqual(query_count_25, 6)
