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
        job_type = self.row.table.job_type
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
        elif job_type == "PERSONAL":
            name_cell = self.row.cells.filter(column__name__in=["TASK_NAME", "TASK NAME", "NAME", "TITLE", "SUBJECT", "TASK"]).first()
            if not name_cell:
                name_cell = self.row.cells.filter(column__data_type="TEXT").first()
            return name_cell.value if name_cell else f"Personal Row {self.row.id}"
        else:
            name_cell = self.row.cells.filter(column__name="TASK_NAME").first()
            return name_cell.value if name_cell else "Unnamed Task"

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
