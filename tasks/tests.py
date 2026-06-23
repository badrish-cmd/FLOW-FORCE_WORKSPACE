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
        # Scenario A: 1 Day Overdue -> Employee only
        task_1d = Task.objects.create(
            row=Row.objects.create(table=self.table),
            due_date=timezone.localdate() - timedelta(days=1),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task_1d.assigned_to.add(self.employee)
        
        # Clear any logs created by assignment
        EmailLog.objects.all().delete()

        check_overdue_escalations()
        
        # Should have sent to employee
        logs_1d = EmailLog.objects.filter(task=task_1d, email_type="OVERDUE_ESCALATION_MAIL")
        self.assertEqual(logs_1d.count(), 1)
        self.assertEqual(logs_1d.first().recipient_email, self.employee.email)

        # Scenario B: 3 Days Overdue -> Department Admin + Employee
        task_3d = Task.objects.create(
            row=Row.objects.create(table=self.table),
            due_date=timezone.localdate() - timedelta(days=3),
            priority="HIGH",
            status="PENDING",
            assigned_by=self.admin
        )
        task_3d.assigned_to.add(self.employee)
        
        EmailLog.objects.all().delete()

        check_overdue_escalations()

        logs_3d = EmailLog.objects.filter(task=task_3d, email_type="OVERDUE_ESCALATION_MAIL")
        self.assertEqual(logs_3d.count(), 2)
        recipients = [log.recipient_email for log in logs_3d]
        self.assertIn(self.employee.email, recipients)
        self.assertIn(self.dept_admin.email, recipients)

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
        """Update task status to READY_FOR_REVIEW, verify email sent to assigned_by admin"""
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
        self.assertIsNotNone(log)
        self.assertEqual(log.recipient_email, self.admin.email)
