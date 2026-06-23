from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


FIXED_COLUMN_KEYS = [
    "S_NO",
    "DATE",
    "DUE_DATE",
    "TASK_NAME",
    "INITIAL_MAIL",
    "ALERT_MAIL",
]

FIXED_COLUMN_LABELS = {
    "S_NO": "S_NO",
    "DATE": "DATE",
    "DUE_DATE": "DUE_DATE",
    "TASK_NAME": "TASK_NAME",
    "INITIAL_MAIL": "INITIAL_MAIL",
    "ALERT_MAIL": "ALERT_MAIL",
}

TASK_STATUS_CHOICES = [
    ("PENDING", "PENDING"),
    ("IN_PROGRESS", "IN_PROGRESS"),
    ("COMPLETED", "COMPLETED"),
    ("ON_HOLD", "ON_HOLD"),
    ("CANCELLED", "CANCELLED"),
]

TASK_PRIORITY_CHOICES = [
    ("LOW", "LOW"),
    ("MEDIUM", "MEDIUM"),
    ("HIGH", "HIGH"),
    ("CRITICAL", "CRITICAL"),
]

MAIL_STATUS_CHOICES = [
    ("NO", "NO"),
    ("YES", "YES"),
]


class Tracker(models.Model):
    department = models.ForeignKey(
        'employee_management.Department',
        on_delete=models.CASCADE,
        related_name='trackers',
        null=True,
        blank=True,
        help_text="Department this tracker belongs to"
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_trackers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    shared_with_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="shared_trackers",
    )
    shared_with_departments = models.ManyToManyField(
        'employee_management.Department',
        blank=True,
        related_name="shared_trackers",
    )
    shared_with_teams = models.ManyToManyField(
        'employee_management.Team',
        blank=True,
        related_name="shared_trackers",
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        dept_name = self.department.name if self.department else "Global"
        return f"{dept_name} - {self.name}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if not self.name:
            if self.department:
                self.name = f"{self.department.name} Task Tracker"
            else:
                self.name = "Custom Task Tracker"
        super().save(*args, **kwargs)
        if is_new:
            from .services import ensure_mandatory_columns

            ensure_mandatory_columns(self)

    def next_s_no(self):
        latest = self.tasks.order_by("-s_no").values_list("s_no", flat=True).first()
        return (latest or 0) + 1


class TrackerColumn(models.Model):
    tracker = models.ForeignKey(
        Tracker,
        on_delete=models.CASCADE,
        related_name="columns",
    )
    key = models.CharField(max_length=60)
    label = models.CharField(max_length=120)
    position = models.PositiveIntegerField(default=0)
    is_fixed = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Custom column builder settings
    column_type = models.CharField(max_length=30, default="TEXT")
    choices = models.TextField(blank=True, default="")
    is_frozen = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    is_read_only = models.BooleanField(default=False)
    is_locked = models.BooleanField(default=False)
    required = models.BooleanField(default=False)
    unique = models.BooleanField(default=False)
    default_value = models.TextField(blank=True, default="")
    permission_level = models.CharField(max_length=30, default="ALL_EDITABLE")

    class Meta:
        ordering = ["position", "id"]
        unique_together = [("tracker", "key")]

    def __str__(self):
        dept_name = self.tracker.department.name if self.tracker.department else "Global"
        return f"{dept_name}: {self.label}"

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = slugify(self.label).replace("-", "_").upper() or "COLUMN"
        super().save(*args, **kwargs)


class TrackerRow(models.Model):
    tracker = models.ForeignKey(
        Tracker,
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    s_no = models.PositiveIntegerField()
    date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    task_name = models.CharField(max_length=255)
    priority = models.CharField(max_length=20, choices=TASK_PRIORITY_CHOICES, default="MEDIUM")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assigned_task_rows",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assigned_by_task_rows",
    )
    status = models.CharField(
        max_length=20,
        choices=TASK_STATUS_CHOICES,
        default="PENDING",
    )
    initial_mail = models.CharField(
        max_length=3,
        choices=MAIL_STATUS_CHOICES,
        default="NO",
    )
    alert_mail = models.CharField(
        max_length=3,
        choices=MAIL_STATUS_CHOICES,
        default="NO",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_task_rows",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["tracker", "s_no"]
        unique_together = [("tracker", "s_no")]

    def __str__(self):
        dept_name = self.tracker.department.name if self.tracker.department else "Global"
        return f"{dept_name} #{self.s_no}: {self.task_name}"

    @property
    def is_overdue(self):
        return self.due_date < timezone.localdate() and self.status not in {"COMPLETED", "CANCELLED"}

    @property
    def is_due_today(self):
        return self.due_date == timezone.localdate() and self.status not in {"COMPLETED", "CANCELLED"}


class TaskAssignment(models.Model):
    ASSIGNMENT_TYPE_CHOICES = [
        ("PRIMARY", "PRIMARY"),
        ("SECONDARY", "SECONDARY"),
        ("WATCHER", "WATCHER"),
    ]

    row = models.ForeignKey(
        TrackerRow,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="task_assignments",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_assignment_actions",
    )
    assignment_type = models.CharField(max_length=20, choices=ASSIGNMENT_TYPE_CHOICES, default="PRIMARY")
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("row", "user", "assignment_type")]
        ordering = ["-is_primary", "id"]

    def __str__(self):
        return f"{self.row} -> {self.user} ({self.assignment_type})"


class TrackerCell(models.Model):
    row = models.ForeignKey(
        TrackerRow,
        on_delete=models.CASCADE,
        related_name="cells",
    )
    column = models.ForeignKey(
        TrackerColumn,
        on_delete=models.CASCADE,
        related_name="cells",
    )
    value = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("row", "column")]
        ordering = ["column__position", "id"]

    def __str__(self):
        return f"{self.row_id}:{self.column.label}"


TaskRow = TrackerRow
TaskCell = TrackerCell


class TaskComment(models.Model):
    row = models.ForeignKey(
        TrackerRow,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
    )
    content = models.TextField()
    mentions = models.JSONField(default=list, blank=True)
    internal = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="comments_created",
    )
    is_pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Comment on {self.row} by {self.created_by}"


def attachment_upload_to(instance, filename):
    return f"task_attachments/tracker_{instance.row.tracker_id}/row_{instance.row_id}/{filename}"


class TaskAttachment(models.Model):
    row = models.ForeignKey(
        TrackerRow,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to=attachment_upload_to)
    original_name = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attachments_uploaded",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_name or self.file.name


class TaskHistory(models.Model):
    row = models.ForeignKey(
        TrackerRow,
        on_delete=models.CASCADE,
        related_name="history",
    )
    action = models.CharField(max_length=120)
    field_name = models.CharField(max_length=120, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="history_actions",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} on {self.row}"


class Notification(models.Model):
    NOTIF_TYPES = [
        ("ASSIGNMENT", "ASSIGNMENT"),
        ("ALERT", "ALERT"),
        ("ESCALATION", "ESCALATION"),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    row = models.ForeignKey(
        TrackerRow,
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
    )
    notif_type = models.CharField(max_length=40, choices=NOTIF_TYPES)
    payload = models.JSONField(default=dict, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-sent_at", "-id"]

    def __str__(self):
        return f"{self.notif_type} -> {self.user}"


class TaskFilter(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="task_filters",
    )
    tracker = models.ForeignKey(
        Tracker,
        on_delete=models.CASCADE,
        related_name="saved_filters",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=120)
    query_params = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        unique_together = [("user", "tracker", "name")]

    def __str__(self):
        return self.name


class EmailLog(models.Model):
    """
    Tracks outgoing emails, their delivery status, error logs, and retry attempts.
    """
    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(max_length=20, choices=[("SENT", "SENT"), ("FAILED", "FAILED")], default="SENT")
    error_message = models.TextField(blank=True, null=True)
    retry_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.recipient} - {self.subject} ({self.status})"
