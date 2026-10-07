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


from tasks.models import Announcement, AnnouncementRead
from tasks.context_processors import global_context
from django.test import RequestFactory


class AnnouncementTestCase(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Announcements Dept", slug="announcements-dept")
        self.admin = User.objects.create_user(
            email="ann_admin@flow-force.com",
            password="testpassword",
            full_name="Announcement Admin",
            role="ADMIN",
            department=self.dept,
            status="APPROVED"
        )
        self.super_admin = User.objects.create_user(
            email="ann_super@flow-force.com",
            password="testpassword",
            full_name="Announcement Super Admin",
            role="SUPER_ADMIN",
            status="APPROVED"
        )
        self.employee_a = User.objects.create_user(
            email="employee_a@flow-force.com",
            password="testpassword",
            full_name="Employee A",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.employee_b = User.objects.create_user(
            email="employee_b@flow-force.com",
            password="testpassword",
            full_name="Employee B",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED"
        )
        self.factory = RequestFactory()

    def test_admin_can_create_announcement(self):
        self.client.force_login(self.admin)
        resp = self.client.post("/announcements/create/", {
            "title": "Phase 3C Release",
            "category": "IMPROVEMENT",
            "content": "Cell editing optimizations released.",
            "bullet_points": "Faster cell save\nAtomic sync",
            "is_published": "on",
        })
        self.assertEqual(resp.status_code, 302)
        ann = Announcement.objects.filter(title="Phase 3C Release").first()
        self.assertIsNotNone(ann)
        self.assertEqual(ann.category, "IMPROVEMENT")
        self.assertTrue(ann.is_published)
        self.assertIsNotNone(ann.published_at)
        self.assertEqual(ann.created_by, self.admin)
        self.assertEqual(ann.bullet_list, ["Faster cell save", "Atomic sync"])

    def test_non_admin_cannot_create_announcement(self):
        self.client.force_login(self.employee_a)
        resp = self.client.post("/announcements/create/", {
            "title": "Unauthorized Announcement",
            "category": "FEATURE",
            "content": "Should not be saved.",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Announcement.objects.filter(title="Unauthorized Announcement").exists())

    def test_draft_announcement_is_invisible_to_employees(self):
        draft = Announcement.objects.create(
            title="Draft Secret Update",
            content="Not ready for employees.",
            is_published=False,
            created_by=self.admin
        )
        self.client.force_login(self.employee_a)
        resp = self.client.get("/announcements/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Draft Secret Update")

        req = self.factory.get("/")
        req.user = self.employee_a
        ctx = global_context(req)
        self.assertEqual(ctx["active_announcement"], None)

    def test_published_announcement_appears_to_employees(self):
        ann = Announcement.objects.create(
            title="Published Work Notes",
            content="All employees can view.",
            is_published=True,
            published_at=timezone.now(),
            created_by=self.admin
        )
        self.client.force_login(self.employee_a)
        resp = self.client.get("/announcements/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Published Work Notes")

    def test_employee_sees_unread_announcement_via_context(self):
        ann = Announcement.objects.create(
            title="Exciting Feature",
            content="Check out the update.",
            is_published=True,
            published_at=timezone.now(),
            created_by=self.admin
        )
        req = self.factory.get("/")
        req.user = self.employee_a
        ctx = global_context(req)
        self.assertEqual(ctx["active_announcement"], ann)

    def test_employee_acknowledges_announcement(self):
        ann = Announcement.objects.create(
            title="Acknowledge Me",
            content="Read me please.",
            is_published=True,
            published_at=timezone.now(),
            created_by=self.admin
        )
        self.client.force_login(self.employee_a)
        resp = self.client.post(f"/announcements/{ann.id}/acknowledge/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(AnnouncementRead.objects.filter(announcement=ann, employee=self.employee_a).exists())

    def test_acknowledged_announcement_does_not_appear_again(self):
        ann = Announcement.objects.create(
            title="Pop Up Once Only",
            content="Should never popup again after Got it.",
            is_published=True,
            published_at=timezone.now(),
            created_by=self.admin
        )
        AnnouncementRead.objects.create(announcement=ann, employee=self.employee_a)

        req = self.factory.get("/")
        req.user = self.employee_a
        ctx = global_context(req)
        self.assertEqual(ctx["active_announcement"], None)

    def test_employee_a_acknowledging_does_not_affect_employee_b(self):
        ann = Announcement.objects.create(
            title="Per Employee Status",
            content="Independent acknowledgement.",
            is_published=True,
            published_at=timezone.now(),
            created_by=self.admin
        )
        AnnouncementRead.objects.create(announcement=ann, employee=self.employee_a)

        req_b = self.factory.get("/")
        req_b.user = self.employee_b
        ctx_b = global_context(req_b)
        self.assertEqual(ctx_b["active_announcement"], ann)

    def test_multiple_announcements_handled_correctly_and_latest_selected_first(self):
        older = Announcement.objects.create(
            title="Older Announcement",
            content="First released.",
            is_published=True,
            published_at=timezone.now() - timedelta(days=2),
            created_by=self.admin
        )
        newer = Announcement.objects.create(
            title="Newer Announcement",
            content="Second released.",
            is_published=True,
            published_at=timezone.now() - timedelta(days=1),
            created_by=self.admin
        )

        req = self.factory.get("/")
        req.user = self.employee_a

        # 1. Latest announcement is returned first
        ctx1 = global_context(req)
        self.assertEqual(ctx1["active_announcement"], newer)

        # 2. Acknowledge newer
        AnnouncementRead.objects.create(announcement=newer, employee=self.employee_a)

        # 3. Older announcement is returned next
        ctx2 = global_context(req)
        self.assertEqual(ctx2["active_announcement"], older)

        # 4. Acknowledge older
        AnnouncementRead.objects.create(announcement=older, employee=self.employee_a)

        # 5. No unread announcements remain
        ctx3 = global_context(req)
        self.assertEqual(ctx3["active_announcement"], None)

    def test_admin_can_publish_unpublish(self):
        ann = Announcement.objects.create(
            title="Toggle Me",
            content="State flipping.",
            is_published=False,
            created_by=self.admin
        )
        self.client.force_login(self.admin)

        # Toggle to published
        resp1 = self.client.post(f"/announcements/{ann.id}/toggle-publish/")
        self.assertEqual(resp1.status_code, 302)
        ann.refresh_from_db()
        self.assertTrue(ann.is_published)
        self.assertIsNotNone(ann.published_at)

        # Toggle to draft
        resp2 = self.client.post(f"/announcements/{ann.id}/toggle-publish/")
        self.assertEqual(resp2.status_code, 302)
        ann.refresh_from_db()
        self.assertFalse(ann.is_published)

    def test_non_admin_cannot_publish_unpublish(self):
        ann = Announcement.objects.create(
            title="Employee Cannot Toggle",
            content="Protected action.",
            is_published=False,
            created_by=self.admin
        )
        self.client.force_login(self.employee_a)
        resp = self.client.post(f"/announcements/{ann.id}/toggle-publish/")
        self.assertEqual(resp.status_code, 403)
        ann.refresh_from_db()
        self.assertFalse(ann.is_published)

    def test_admin_can_edit_announcement(self):
        ann = Announcement.objects.create(
            title="Original Title",
            content="Original Content",
            category="BUGFIX",
            is_published=False,
            created_by=self.admin
        )
        self.client.force_login(self.admin)
        resp = self.client.post(f"/announcements/{ann.id}/edit/", {
            "title": "Updated Title",
            "content": "Updated Content",
            "category": "PERFORMANCE",
            "bullet_points": "Point A\nPoint B",
            "is_published": "on",
        })
        self.assertEqual(resp.status_code, 302)
        ann.refresh_from_db()
        self.assertEqual(ann.title, "Updated Title")
        self.assertEqual(ann.content, "Updated Content")
        self.assertEqual(ann.category, "PERFORMANCE")
        self.assertTrue(ann.is_published)

    def test_non_admin_cannot_edit_or_delete_announcement(self):
        ann = Announcement.objects.create(
            title="Immutable by Employees",
            content="Cannot touch.",
            is_published=True,
            created_by=self.admin
        )
        self.client.force_login(self.employee_a)

        # Edit attempt
        resp_edit = self.client.post(f"/announcements/{ann.id}/edit/", {
            "title": "Hacked Title",
            "content": "Hacked Content",
        })
        self.assertEqual(resp_edit.status_code, 403)

        # Delete attempt
        resp_del = self.client.post(f"/announcements/{ann.id}/delete/")
        self.assertEqual(resp_del.status_code, 403)

        ann.refresh_from_db()
        self.assertEqual(ann.title, "Immutable by Employees")

    def test_admin_can_delete_announcement(self):
        ann = Announcement.objects.create(
            title="To Be Deleted",
            content="Goodbye.",
            is_published=True,
            created_by=self.admin
        )
        self.client.force_login(self.admin)
        resp = self.client.post(f"/announcements/{ann.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Announcement.objects.filter(id=ann.id).exists())

    def test_duplicate_acknowledgement_does_not_create_duplicate_records(self):
        ann = Announcement.objects.create(
            title="Idempotent Ack",
            content="Ack twice.",
            is_published=True,
            published_at=timezone.now(),
            created_by=self.admin
        )
        self.client.force_login(self.employee_a)

        self.client.post(f"/announcements/{ann.id}/acknowledge/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.client.post(f"/announcements/{ann.id}/acknowledge/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        read_count = AnnouncementRead.objects.filter(announcement=ann, employee=self.employee_a).count()
        self.assertEqual(read_count, 1)

    def test_existing_task_notification_functionality_remains_unchanged(self):
        notif = Notification.objects.create(
            user=self.employee_a,
            title="Task Notification 101",
            description="You have a new task assigned.",
            type="ASSIGNED",
            is_read=False
        )
        req = self.factory.get("/")
        req.user = self.employee_a
        ctx = global_context(req)

        self.assertEqual(ctx["task_notifications_unread"], 1)
        self.assertEqual(len(ctx["unread_notifications"]), 1)
        self.assertEqual(ctx["unread_notifications"][0]["title"], "Task Notification 101")

    def test_bullet_list_property(self):
        ann = Announcement(
            bullet_points="   Leading/trailing spaces   \n\n   Bullet 2   \n\n"
        )
        self.assertEqual(ann.bullet_list, ["Leading/trailing spaces", "Bullet 2"])

    def test_admin_manage_dashboard_access_and_tabs(self):
        Announcement.objects.create(title="Pub 1", content="C1", is_published=True, published_at=timezone.now(), created_by=self.admin)
        Announcement.objects.create(title="Draft 1", content="C2", is_published=False, created_by=self.admin)

        # 1. Non-admin gets 403
        self.client.force_login(self.employee_a)
        res_emp = self.client.get("/announcements/manage/")
        self.assertEqual(res_emp.status_code, 403)

        # 2. Admin gets 200
        self.client.force_login(self.admin)
        res_all = self.client.get("/announcements/manage/")
        self.assertEqual(res_all.status_code, 200)
        self.assertEqual(res_all.context["total_count"], 2)
        self.assertEqual(res_all.context["published_count"], 1)
        self.assertEqual(res_all.context["draft_count"], 1)

        # 3. Filter tabs
        res_pub = self.client.get("/announcements/manage/?tab=published")
        self.assertEqual(len(res_pub.context["announcements"]), 1)
        self.assertEqual(res_pub.context["announcements"][0]["item"].title, "Pub 1")

    def test_super_admin_receives_unread_published_announcement(self):
        # 1. Admin creates and publishes an announcement
        ann = Announcement.objects.create(
            title="Super Admin Alert",
            content="Important update for everyone including super admins.",
            is_published=True,
            published_at=timezone.now(),
            created_by=self.admin
        )

        # 2. Super admin gets active_announcement in global_context
        req_super = self.factory.get("/")
        req_super.user = self.super_admin
        ctx_super = global_context(req_super)
        self.assertEqual(ctx_super["active_announcement"], ann)

        # 3. Super admin can see announcement in history (/announcements/)
        self.client.force_login(self.super_admin)
        resp_list = self.client.get("/announcements/")
        self.assertEqual(resp_list.status_code, 200)
        self.assertContains(resp_list, "Super Admin Alert")

        # 4. Super admin can acknowledge
        ack_resp = self.client.post(f"/announcements/{ann.id}/acknowledge/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(ack_resp.status_code, 200)

        # 5. After acknowledge, super admin no longer receives it in global_context
        ctx_super_after = global_context(req_super)
        self.assertEqual(ctx_super_after["active_announcement"], None)

        # 6. Other employees (e.g. employee_a) who have not acknowledged still see it
        req_emp = self.factory.get("/")
        req_emp.user = self.employee_a
        ctx_emp = global_context(req_emp)
        self.assertEqual(ctx_emp["active_announcement"], ann)

    def test_publishing_does_not_auto_mark_as_read_for_creator_or_super_admin(self):
        # Super admin creates and publishes announcement
        self.client.force_login(self.super_admin)
        resp = self.client.post("/announcements/create/", {
            "title": "Created By Super Admin",
            "category": "SECURITY",
            "content": "Security patch applied.",
            "is_published": "on",
        })
        self.assertEqual(resp.status_code, 302)
        ann = Announcement.objects.filter(title="Created By Super Admin").first()
        self.assertIsNotNone(ann)
        self.assertTrue(ann.is_published)

        # AnnouncementRead must not exist for creator (super_admin)
        self.assertFalse(AnnouncementRead.objects.filter(announcement=ann, employee=self.super_admin).exists())

        # Super admin should see it in global_context
        req = self.factory.get("/")
        req.user = self.super_admin
        ctx = global_context(req)
        self.assertEqual(ctx["active_announcement"], ann)


class ChannelsFoundationRegressionTestCase(TestCase):
    """
    PHASE REALTIME-1: Verifies Django Channels foundation infrastructure.
    - Daphne and channels in INSTALLED_APPS
    - ASGI application imports and initializes ProtocolTypeRouter
    - WSGI application remains unchanged
    - Channel layer resolves and initializes correctly with Redis DB 2
    """
    def test_channels_and_daphne_installed(self):
        from django.conf import settings
        self.assertIn('daphne', settings.INSTALLED_APPS)
        self.assertIn('channels', settings.INSTALLED_APPS)
        self.assertEqual(settings.INSTALLED_APPS[0], 'daphne')

    def test_asgi_and_wsgi_settings(self):
        from django.conf import settings
        self.assertEqual(settings.ASGI_APPLICATION, 'flowforce.asgi.application')
        self.assertEqual(settings.WSGI_APPLICATION, 'flowforce.wsgi.application')

    def test_asgi_application_initialization(self):
        from flowforce.asgi import application
        from channels.routing import ProtocolTypeRouter
        self.assertIsInstance(application, ProtocolTypeRouter)
        self.assertIn('http', application.application_mapping)
        self.assertIn('websocket', application.application_mapping)

    def test_channel_layer_configuration(self):
        from django.conf import settings
        from channels.layers import get_channel_layer
        from channels_redis.core import RedisChannelLayer
        layer = get_channel_layer()
        self.assertIsInstance(layer, RedisChannelLayer)
        self.assertIn('redis://127.0.0.1:6379/2', settings.CHANNELS_REDIS_URL)

    def test_wsgi_application_functional(self):
        from flowforce.wsgi import application
        from django.core.handlers.wsgi import WSGIHandler
        self.assertIsInstance(application, WSGIHandler)
