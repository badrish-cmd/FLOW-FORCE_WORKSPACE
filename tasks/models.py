from django.db import models
from django.conf import settings
from tables.models import Row

class Task(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("IN_PROGRESS", "In Progress"),
        ("READY_FOR_REVIEW", "Ready for Review"),
        ("APPROVED", "Approved"),
        ("COMPLETED", "Completed"),
        ("RETURNED", "Returned"),
        ("NOT_RETURNED", "Not Returned"),
    ]

    PRIORITY_CHOICES = [
        ("LOW", "Low"),
        ("MEDIUM", "Medium"),
        ("HIGH", "High"),
        ("CRITICAL", "Critical"),
    ]

    row = models.OneToOneField(
        Row,
        on_delete=models.CASCADE,
        related_name="task",
        db_column="row_fk"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tasks"
    )
    assigned_to = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="tasks_assigned"
    )
    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default="PENDING",
        db_index=True
    )
    due_date = models.DateField(null=True, blank=True, db_index=True)
    priority = models.CharField(
        max_length=50,
        choices=PRIORITY_CHOICES,
        default="MEDIUM",
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    initial_mail_sent = models.BooleanField(default=False)
    alert_mail_sent = models.BooleanField(default=False)
    last_escalation_level = models.IntegerField(default=0)
    last_escalation_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Task for Row {self.row_id} ({self.status})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.core.cache import cache
        from django.utils import timezone
        today_str = timezone.localdate().isoformat()
        if self.row_id:
            try:
                table_id = self.row.table_id
                cache.delete(f"table_stats_{table_id}_{today_str}")
            except Exception:
                pass

    def delete(self, *args, **kwargs):
        try:
            table_id = self.row.table_id
        except Exception:
            table_id = None
        super().delete(*args, **kwargs)
        if table_id:
            from django.core.cache import cache
            from django.utils import timezone
            today_str = timezone.localdate().isoformat()
            cache.delete(f"table_stats_{table_id}_{today_str}")

    @property
    def is_overdue(self):
        from django.utils import timezone
        if self.status in ["COMPLETED", "APPROVED"]:
            return False
        if not self.due_date:
            return False
        return self.due_date < timezone.localdate()

    @property
    def task_name(self):
        try:
            job_type = self.row.table.job_type
        except Exception:
            return "Unnamed Task"

        # If cells are already prefetched on row, inspect in-memory to prevent N+1 queries
        if hasattr(self.row, '_prefetched_objects_cache') and 'cells' in self.row._prefetched_objects_cache:
            cells = list(self.row.cells.all())

            def find_cell(names, by_type=False):
                if by_type:
                    for c in cells:
                        if getattr(c.column, 'data_type', None) == "TEXT":
                            return c
                for target in names:
                    for c in cells:
                        col_name = getattr(c.column, 'name', '')
                        if col_name and col_name.upper() == target.upper():
                            return c
                return None

            if job_type == "SALES":
                cell = find_cell(["CUSTOMER_NAME"]) or find_cell(["TASK_NAME"])
                return cell.value if (cell and cell.value) else "Unnamed Task"
            elif job_type == "LIST_PID":
                cell = find_cell(["ENQUIRY_NO", "ENQUIRY_NO/QUOTATION_NO", "ENQUIRY_NO_QUOTATION_NO", "ENQUIRY NUMBER", "ENQUIRY NO", "ENQUIRY_NO / QUOTATION_NO"]) or find_cell(["PID"])
                return cell.value if (cell and cell.value) else "Unnamed Task"
            elif job_type == "LOGS":
                cell = find_cell(["TOOL_NAME", "TOOL NAME", "TOOL"]) or find_cell(["TASK_NAME"])
                return cell.value if (cell and cell.value) else "Unnamed Tool"
            elif job_type == "PERSONAL":
                cell = find_cell(["TASK_NAME", "TASK NAME", "NAME", "TITLE", "SUBJECT", "TASK"]) or find_cell([], by_type=True)
                return cell.value if (cell and cell.value) else f"Personal Row {self.row.id}"
            else:
                cell = find_cell(["TASK_NAME"])
                return cell.value if (cell and cell.value) else "Unnamed Task"

        if job_type == "SALES":
            name_cell = self.row.cells.filter(column__name="CUSTOMER_NAME").first()
            if not name_cell:
                name_cell = self.row.cells.filter(column__name="TASK_NAME").first()
            return name_cell.value if name_cell else "Unnamed Task"
        elif job_type == "LIST_PID":
            name_cell = self.row.cells.filter(
                column__name__in=["ENQUIRY_NO", "ENQUIRY_NO/QUOTATION_NO", "ENQUIRY_NO_QUOTATION_NO", "ENQUIRY NUMBER", "ENQUIRY NO", "ENQUIRY_NO / QUOTATION_NO"]
            ).first()
            if not name_cell:
                name_cell = self.row.cells.filter(column__name="PID").first()
            return name_cell.value if name_cell else "Unnamed Task"
        elif job_type == "LOGS":
            name_cell = self.row.cells.filter(column__name__in=["TOOL_NAME", "TOOL NAME", "TOOL"]).first()
            if not name_cell:
                name_cell = self.row.cells.filter(column__name="TASK_NAME").first()
            return name_cell.value if name_cell else "Unnamed Tool"
        elif job_type == "PERSONAL":
            name_cell = self.row.cells.filter(column__name__in=["TASK_NAME", "TASK NAME", "NAME", "TITLE", "SUBJECT", "TASK"]).first()
            if not name_cell:
                name_cell = self.row.cells.filter(column__data_type="TEXT").first()
            return name_cell.value if name_cell else f"Personal Row {self.row.id}"
        else:
            name_cell = self.row.cells.filter(column__name="TASK_NAME").first()
            return name_cell.value if name_cell else "Unnamed Task"

    @property
    def pid_data(self):
        cell = self.row.cells.filter(column__name__iexact="PID").first()
        return cell.value if (cell and cell.value) else None

    @property
    def customer_name_data(self):
        cell = self.row.cells.filter(column__name__in=["CUSTOMER_NAME", "CUSTOMER NAME", "CUSTOMER"]).first()
        return cell.value if (cell and cell.value) else None

    @property
    def task_name_data(self):
        cell = self.row.cells.filter(column__name__in=["TASK_NAME", "TASK NAME"]).first()
        return cell.value if (cell and cell.value) else None

    @property
    def tool_name_data(self):
        cell = self.row.cells.filter(column__name__in=["TOOL_NAME", "TOOL NAME"]).first()
        return cell.value if (cell and cell.value) else None

    @property
    def issued_date_data(self):
        cell = self.row.cells.filter(column__name__in=["ISSUE_DATE", "ISSUE DATE", "DATE"]).first()
        return cell.value if (cell and cell.value) else None

    @property
    def return_date_data(self):
        cell = self.row.cells.filter(column__name__in=["RETURN_DATE", "RETURN DATE", "DUE_DATE"]).first()
        return cell.value if (cell and cell.value) else None

    @property
    def issued_by_data(self):
        cell = self.row.cells.filter(column__name__in=["ISSUED_BY", "ISSUED BY"]).first()
        return cell.value if (cell and cell.value) else None

    @property
    def received_by_data(self):
        cell = self.row.cells.filter(column__name__in=["RECEIVED_BY", "RECEIVED BY"]).first()
        return cell.value if (cell and cell.value) else None

    @property
    def table_name(self):
        return self.row.table.name

class TaskComment(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="comments",
        db_column="task_fk"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="task_comments"
    )
    content = models.TextField()
    is_internal_note = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        author_name = self.author.email if self.author else "System"
        return f"Comment by {author_name} on Task {self.task_id}"

class ActivityLog(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="activity_logs",
        db_column="task_fk"
    )
    action = models.CharField(max_length=255)
    details = models.JSONField(default=dict, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="task_activity_logs"
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        user_name = self.user.email if self.user else "System"
        return f"{self.action} by {user_name} at {self.timestamp}"

class Notification(models.Model):
    NOTIFICATION_TYPES = [
        ("ASSIGNED", "Task Assigned"),
        ("DUE_TODAY", "Due Today"),
        ("COMMENT", "Comment Mention"),
        ("REVIEW", "Review Request"),
        ("SYSTEM", "System Alert"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="app_notifications"
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
        db_column="task_fk"
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    type = models.CharField(
        max_length=50,
        choices=NOTIFICATION_TYPES,
        default="SYSTEM"
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Notif for {self.user.email}: {self.title}"

class EmailLog(models.Model):
    EMAIL_TYPES = [
        ("INITIAL_MAIL", "Initial Assignment Mail"),
        ("ALERT_MAIL", "Daily Alert Mail"),
        ("REVIEW_REQUEST_MAIL", "Review Request Mail"),
        ("APPROVAL_STATUS_MAIL", "Approval Status Mail"),
        ("OVERDUE_ESCALATION_MAIL", "Overdue Escalation Mail"),
    ]

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("SENT", "Sent"),
        ("FAILED", "Failed"),
    ]

    recipient_email = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField(blank=True, null=True)
    task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_logs",
        db_column="task_fk"
    )
    email_type = models.CharField(
        max_length=50,
        choices=EMAIL_TYPES
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING"
    )
    error_message = models.TextField(blank=True, null=True)
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    next_retry_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email_type} to {self.recipient_email} - {self.status}"

class TaskFollowUp(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="follow_ups",
        db_column="task_fk"
    )
    follow_up_date = models.DateField(
        help_text="The date this follow-up was scheduled and conducted"
    )
    discussed_points = models.TextField(
        help_text="Detailed points of what was discussed"
    )
    next_follow_up_date = models.DateField(
        null=True,
        blank=True,
        help_text="The scheduled next follow-up date (if continuing)"
    )
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="entered_follow_ups"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-follow_up_date", "-created_at"]

    def __str__(self):
        return f"Follow-up for Task {self.task_id} on {self.follow_up_date}"


class Announcement(models.Model):
    CATEGORY_CHOICES = [
        ("FEATURE", "New Feature"),
        ("IMPROVEMENT", "Improvement"),
        ("BUGFIX", "Bug Fix"),
        ("PERFORMANCE", "Performance"),
        ("SECURITY", "Security"),
        ("MAINTENANCE", "Maintenance"),
    ]

    title = models.CharField(max_length=255)
    category = models.CharField(
        max_length=30,
        choices=CATEGORY_CHOICES,
        default="IMPROVEMENT"
    )
    content = models.TextField(help_text="Detailed description of the update")
    bullet_points = models.TextField(
        blank=True,
        default="",
        help_text="One bullet point per line"
    )
    is_published = models.BooleanField(default=False, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_announcements"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]

    def __str__(self):
        return self.title

    @property
    def bullet_list(self):
        """Returns non-empty bullet points stripped of leading/trailing whitespace."""
        if not self.bullet_points:
            return []
        return [line.strip() for line in self.bullet_points.splitlines() if line.strip()]


class AnnouncementRead(models.Model):
    announcement = models.ForeignKey(
        Announcement,
        on_delete=models.CASCADE,
        related_name="reads"
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="announcement_reads"
    )
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("announcement", "employee")]
        indexes = [
            models.Index(fields=["employee", "announcement"]),
        ]

    def __str__(self):
        return f"Read {self.announcement_id} by {self.employee.email}"
