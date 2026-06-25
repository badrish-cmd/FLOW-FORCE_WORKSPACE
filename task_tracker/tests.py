from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from auth_app.models import EmployeeUser
from .models import FIXED_COLUMN_KEYS
from .models import TaskRow
from .models import Tracker
from .services import apply_task_filters
from .services import send_due_task_alerts


class TaskTrackerServiceTests(TestCase):
    def setUp(self):
        from employee_management.models import Department
        self.dept, _ = Department.objects.get_or_create(name="Engineering")
        self.super_admin = EmployeeUser.objects.create_user(
            email="admin@example.com",
            password="pass12345",
            full_name="Super Admin",
            role="SUPER_ADMIN",
            department=self.dept,
            status="APPROVED",
            is_staff=True,
        )
        self.employee = EmployeeUser.objects.create_user(
            email="employee@example.com",
            password="pass12345",
            full_name="Employee One",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED",
        )
        self.tracker = Tracker.objects.create(
            department=self.dept,
            name="Engineering Daily Tracker",
            created_by=self.super_admin,
        )

    def test_mandatory_columns_are_created(self):
        columns = list(self.tracker.columns.order_by("position"))
        self.assertEqual([column.key for column in columns[: len(FIXED_COLUMN_KEYS)]], FIXED_COLUMN_KEYS)
        self.assertTrue(all(column.is_fixed for column in columns[: len(FIXED_COLUMN_KEYS)]))

    @patch("task_tracker.services.send_mail")
    def test_due_task_alert_marks_alert_mail_yes(self, mock_send_mail):
        today = timezone.localdate()
        TaskRow.objects.create(
            tracker=self.tracker,
            s_no=1,
            date=today,
            due_date=today,
            task_name="Daily Check",
            priority="MEDIUM",
            assigned_to=self.employee,
            assigned_by=self.super_admin,
            status="PENDING",
        )

        sent = send_due_task_alerts(today)
        task = TaskRow.objects.get(task_name="Daily Check")

        self.assertEqual(sent, 1)
        self.assertEqual(task.alert_mail, "YES")
        mock_send_mail.assert_called_once()

    def test_apply_task_filters_supports_search_and_overdue(self):
        today = timezone.localdate()
        overdue_task = TaskRow.objects.create(
            tracker=self.tracker,
            s_no=1,
            date=today - timedelta(days=2),
            due_date=today - timedelta(days=1),
            task_name="Fix wiring",
            priority="HIGH",
            assigned_to=self.employee,
            assigned_by=self.super_admin,
            status="PENDING",
        )
        completed_task = TaskRow.objects.create(
            tracker=self.tracker,
            s_no=2,
            date=today,
            due_date=today,
            task_name="Archive docs",
            priority="LOW",
            assigned_to=self.employee,
            assigned_by=self.super_admin,
            status="COMPLETED",
        )

        queryset = TaskRow.objects.all().select_related("tracker", "assigned_to", "assigned_by")
        filtered = apply_task_filters(queryset, {"search": "wiring", "overdue": True}, tracker=self.tracker)

        self.assertEqual(list(filtered), [overdue_task])
        self.assertNotIn(completed_task, filtered)

    def test_tracker_sharing_logic(self):
        from employee_management.models import Team
        from .services import get_visible_trackers

        # Create another user and another tracker
        user2 = EmployeeUser.objects.create_user(
            email="user2@example.com",
            password="pass12345",
            full_name="User Two",
            role="EMPLOYEE",
            department=self.dept,
            status="APPROVED",
        )
        tracker2 = Tracker.objects.create(
            department=self.dept,
            name="Restricted Tracker",
            created_by=self.super_admin,
        )

        # By default, user2 cannot see tracker2 because they didn't create it and it is not shared
        # (Since tracker2 is created by super_admin, let's test if user2 can see it)
        # Note: get_visible_trackers for employee returns trackers where they have tasks OR shared
        self.assertNotIn(tracker2, get_visible_trackers(user2))

        # Share tracker2 with user2 directly
        tracker2.shared_with_users.add(user2)
        self.assertIn(tracker2, get_visible_trackers(user2))

        # Remove direct share, add team share
        tracker2.shared_with_users.remove(user2)
        team = Team.objects.create(name="Devs")
        team.members.add(user2)
        tracker2.shared_with_teams.add(team)
        self.assertIn(tracker2, get_visible_trackers(user2))

    @patch("django.core.mail.send_mail")
    def test_ajax_cell_edit_and_review_workflow(self, mock_send_mail):
        from django.urls import reverse
        import json
        from .models import Notification

        task = TaskRow.objects.create(
            tracker=self.tracker,
            s_no=1,
            date=timezone.localdate(),
            due_date=timezone.localdate() + timedelta(days=2),
            task_name="Verify Cell Editing",
            priority="MEDIUM",
            assigned_to=self.employee,
            assigned_by=self.super_admin,
            status="IN_PROGRESS",
        )

        # Edit TASK_NAME via AJAX
        self.client.force_login(self.super_admin)
        url = reverse("task_tracker:ajax_cell_edit", kwargs={"tracker_id": self.tracker.id, "task_id": task.id})
        response = self.client.post(
            url,
            data=json.dumps({"column_key": "TASK_NAME", "value": "Updated Cell Name"}),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.task_name, "Updated Cell Name")

        # Employee submits task for review
        self.client.force_login(self.employee)
        review_url = reverse("task_tracker:task_mark_review", kwargs={"tracker_id": self.tracker.id, "task_id": task.id})
        response = self.client.post(review_url)
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "READY_FOR_REVIEW")
        
        # Verify an alert notification is created for assigner (super_admin)
        self.assertTrue(Notification.objects.filter(user=self.super_admin, row=task).exists())

        # Admin reviews task and approves it
        self.client.force_login(self.super_admin)
        decide_url = reverse("task_tracker:task_review_decide", kwargs={"tracker_id": self.tracker.id, "task_id": task.id})
        response = self.client.post(
            decide_url,
            data=json.dumps({"decision": "APPROVED", "notes": "LGTM"}),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "COMPLETED")

    @patch("task_tracker.services.send_mail")
    def test_overdue_escalations_and_retry(self, mock_send_mail):
        from .services import send_overdue_escalations
        from .models import EmailLog, Notification
        from django.core.management import call_command

        today = timezone.localdate()
        task = TaskRow.objects.create(
            tracker=self.tracker,
            s_no=1,
            date=today - timedelta(days=7),
            due_date=today - timedelta(days=6), # exactly 6 days overdue -> EMPLOYEE level
            task_name="Late Task",
            priority="HIGH",
            assigned_to=self.employee,
            assigned_by=self.super_admin,
            status="IN_PROGRESS",
        )

        # Create department admin user
        dept_admin = EmployeeUser.objects.create_user(
            email="deptadmin@example.com",
            password="pass12345",
            full_name="Dept Admin",
            role="DEPARTMENT_ADMIN",
            department=self.dept,
            status="APPROVED",
        )

        send_overdue_escalations()
        
        # Check that employee received escalation notification
        self.assertTrue(Notification.objects.filter(user=self.employee, row=task, notif_type="ESCALATION").exists())
        # Check that department admin did NOT receive it
        self.assertFalse(Notification.objects.filter(user=dept_admin, row=task, notif_type="ESCALATION").exists())
        
        # Check EmailLog table logging
        self.assertTrue(EmailLog.objects.filter(recipient=self.employee.email).exists())

        # Mock a failed email log to test retry command
        failed_log = EmailLog.objects.create(
            recipient="fail@example.com",
            subject="Test Retry",
            body="Retry Body",
            status="FAILED",
            retry_count=0
        )

        # Call run_automation_engine command which includes retry mechanism
        call_command("run_automation_engine")
        
        failed_log.refresh_from_db()
        self.assertEqual(failed_log.status, "SENT")
        self.assertEqual(failed_log.retry_count, 1)
