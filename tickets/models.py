"""
Tickets & Service Jobs module with Synology NAS storage integration.
"""
import os
import re
from datetime import timedelta
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


class Ticket(models.Model):
    TYPE_PROJECT = "PROJECT"
    TYPE_SERVICE = "SERVICE"
    TYPE_CHOICES = [
        (TYPE_PROJECT, "Project Job"),
        (TYPE_SERVICE, "Service Ticket"),
    ]

    PRIORITY_LOW = "LOW"
    PRIORITY_NORMAL = "NORMAL"
    PRIORITY_HIGH = "HIGH"
    PRIORITY_URGENT = "URGENT"
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Low"),
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_HIGH, "High"),
        (PRIORITY_URGENT, "Urgent"),
    ]

    # Project Job Statuses
    STATUS_PO_RECEIVED = "PO_RECEIVED"
    STATUS_ORDERED_FROM_VENDOR = "ORDERED_FROM_VENDOR"
    STATUS_IN_TRANSIT = "IN_TRANSIT"
    STATUS_DELIVERED = "DELIVERED"
    STATUS_INSTALLATION = "INSTALLATION"
    STATUS_HANDOVER = "HANDOVER"
    STATUS_CLOSED = "CLOSED"

    PROJECT_STATUS_CHOICES = [
        (STATUS_PO_RECEIVED, "PO Received"),
        (STATUS_ORDERED_FROM_VENDOR, "Ordered from Vendor"),
        (STATUS_IN_TRANSIT, "In Transit"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_INSTALLATION, "Installation"),
        (STATUS_HANDOVER, "Handover"),
        (STATUS_CLOSED, "Closed"),
    ]

    PROJECT_STATUSES = [s[0] for s in PROJECT_STATUS_CHOICES]

    # Service Ticket Statuses
    STATUS_OPEN = "OPEN"
    STATUS_SITE_VISIT_SCHEDULED = "SITE_VISIT_SCHEDULED"
    STATUS_SITE_VISIT_DONE = "SITE_VISIT_DONE"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_WAITING_PARTS = "WAITING_PARTS"
    STATUS_COMPLETED = "COMPLETED"

    SERVICE_STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_SITE_VISIT_SCHEDULED, "Site Visit Scheduled"),
        (STATUS_SITE_VISIT_DONE, "Site Visit Done"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_WAITING_PARTS, "Waiting Parts"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CLOSED, "Closed"),
    ]

    SERVICE_STATUSES = [s[0] for s in SERVICE_STATUS_CHOICES]

    ALL_STATUS_CHOICES = [
        ("PROJECT", PROJECT_STATUS_CHOICES),
        ("SERVICE", SERVICE_STATUS_CHOICES),
    ]

    # Core Identifiers
    number = models.CharField(max_length=30, unique=True, db_index=True, help_text="e.g. TKT-26-0001")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_PROJECT, db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=40, db_index=True)
    status_changed_at = models.DateTimeField(default=timezone.now)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL, db_index=True)
    parts_purchased = models.BooleanField(default=False, help_text="Checked if service required purchasing parts")

    # Zoho & ERP Linkage
    job_no = models.CharField(max_length=50, blank=True, db_index=True, help_text="Zoho Job No, e.g. 1921")
    enquiry_number = models.CharField(max_length=100, blank=True, db_index=True, help_text="Enquiry number, e.g. ENQ/2026/09/2708")
    quote_number = models.CharField(max_length=100, blank=True, help_text="Quotation number, e.g. 26000245R1")
    sales_order_numbers = models.JSONField(default=list, blank=True, help_text="List of associated Zoho Sales Orders")

    # Customer & Contact Details
    customer_name = models.CharField(max_length=255, blank=True)
    zoho_customer_id = models.CharField(max_length=50, blank=True)
    salesperson_name = models.CharField(max_length=100, blank=True)
    client_contact_name = models.CharField(max_length=100, blank=True)
    client_phone = models.CharField(max_length=50, blank=True)
    client_email = models.CharField(max_length=255, blank=True)
    site_address = models.TextField(blank=True)

    # Warranty
    warranty_start = models.DateField(null=True, blank=True)
    warranty_end = models.DateField(null=True, blank=True)

    # Relationships
    parent_job = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="service_tickets",
        help_text="Link a SERVICE ticket to a parent PROJECT job"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tickets"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_tickets"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Synology Storage
    nas_folder = models.CharField(max_length=500, blank=True, help_text="Full path of this ticket's root folder on Synology NAS")

    # Soft Delete
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["type", "status"]),
            models.Index(fields=["customer_name"]),
            models.Index(fields=["job_no"]),
            models.Index(fields=["enquiry_number"]),
        ]

    def __str__(self):
        return f"{self.number}: {self.title}"

    @classmethod
    def generate_next_number(cls, year=None):
        if year is None:
            year = timezone.now().year % 100
        prefix = f"TKT-{year:02d}-"
        last = cls.objects.filter(number__startswith=prefix).order_by("-number").values_list("number", flat=True).first()
        if last:
            try:
                seq_str = last.split("-")[-1]
                seq = int(seq_str) + 1
            except (ValueError, IndexError):
                seq = 1
        else:
            seq = 1
        return f"{prefix}{seq:04d}"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = self.generate_next_number()
        if not self.status:
            self.status = self.STATUS_PO_RECEIVED if self.type == self.TYPE_PROJECT else self.STATUS_OPEN
        super().save(*args, **kwargs)

    @property
    def is_warranty_active(self):
        if not self.warranty_start or not self.warranty_end:
            return False
        today = timezone.localdate()
        return self.warranty_start <= today <= self.warranty_end

    @property
    def is_warranty_expiring_soon(self):
        if not self.warranty_end:
            return False
        today = timezone.localdate()
        days_left = (self.warranty_end - today).days
        return 0 <= days_left <= 30

    @property
    def warranty_days_remaining(self):
        if not self.warranty_end:
            return None
        today = timezone.localdate()
        return (self.warranty_end - today).days

    @property
    def status_choices(self):
        return self.PROJECT_STATUS_CHOICES if self.type == self.TYPE_PROJECT else self.SERVICE_STATUS_CHOICES

    @property
    def status_list(self):
        return self.PROJECT_STATUSES if self.type == self.TYPE_PROJECT else self.SERVICE_STATUSES

    @property
    def status_index(self):
        statuses = self.status_list
        return statuses.index(self.status) if self.status in statuses else 0

    @property
    def status_label(self):
        return dict(self.status_choices).get(self.status, self.status)


class TicketFile(models.Model):
    CATEGORY_CLIENT_DETAILS = "CLIENT_DETAILS"
    CATEGORY_PHOTO = "PHOTO"
    CATEGORY_SITE_REPORT = "SITE_REPORT"
    CATEGORY_WARRANTY = "WARRANTY"
    CATEGORY_RECEIPT = "RECEIPT"
    CATEGORY_OTHER = "OTHER"

    CATEGORY_CHOICES = [
        (CATEGORY_CLIENT_DETAILS, "01 Client Details"),
        (CATEGORY_PHOTO, "02 Photos"),
        (CATEGORY_SITE_REPORT, "03 Site Reports"),
        (CATEGORY_WARRANTY, "04 Warranty"),
        (CATEGORY_RECEIPT, "05 Receipts"),
        (CATEGORY_OTHER, "Other"),
    ]

    CATEGORY_FOLDER_MAP = {
        CATEGORY_CLIENT_DETAILS: "01 Client Details",
        CATEGORY_PHOTO: "02 Photos",
        CATEGORY_SITE_REPORT: "03 Site Reports",
        CATEGORY_WARRANTY: "04 Warranty",
        CATEGORY_RECEIPT: "05 Receipts",
        CATEGORY_OTHER: "01 Client Details",
    }

    FOLDER_TO_CATEGORY_MAP = {
        "01 client details": CATEGORY_CLIENT_DETAILS,
        "02 photos": CATEGORY_PHOTO,
        "03 site reports": CATEGORY_SITE_REPORT,
        "04 warranty": CATEGORY_WARRANTY,
        "05 receipts": CATEGORY_RECEIPT,
        "photos": CATEGORY_PHOTO,
        "site reports": CATEGORY_SITE_REPORT,
        "warranty": CATEGORY_WARRANTY,
        "receipts": CATEGORY_RECEIPT,
    }

    SOURCE_UPLOAD = "UPLOAD"
    SOURCE_ZIP = "ZIP"
    SOURCE_LINK = "LINK"
    SOURCE_NAS_SCAN = "NAS_SCAN"
    SOURCE_CHOICES = [
        (SOURCE_UPLOAD, "Direct Upload"),
        (SOURCE_ZIP, "Extracted from ZIP"),
        (SOURCE_LINK, "Downloaded from Link"),
        (SOURCE_NAS_SCAN, "Synology NAS Scan"),
    ]

    SYNC_SYNCED = "SYNCED"
    SYNC_PENDING = "PENDING"
    SYNC_FAILED = "FAILED"
    SYNC_CHOICES = [
        (SYNC_SYNCED, "Synced to Synology"),
        (SYNC_PENDING, "Pending Sync"),
        (SYNC_FAILED, "Sync Failed"),
    ]

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="files")
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER, db_index=True)
    original_name = models.CharField(max_length=255)
    stored_name = models.CharField(max_length=255)
    nas_path = models.CharField(max_length=600, blank=True, db_index=True)
    size_bytes = models.BigIntegerField(default=0)
    mime_type = models.CharField(max_length=100, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    thumbnail = models.FileField(upload_to="tickets/thumbnails/%Y/%m/", blank=True)

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_UPLOAD)
    sync_status = models.CharField(max_length=20, choices=SYNC_CHOICES, default=SYNC_PENDING, db_index=True)
    sync_error = models.TextField(blank=True)
    local_temp_path = models.CharField(max_length=500, blank=True, help_text="Local temporary path while sync is pending")

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    is_hidden = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["ticket", "category", "is_hidden"]),
            models.Index(fields=["ticket", "sha256"]),
        ]

    def __str__(self):
        return f"{self.ticket.number} - {self.original_name} ({self.category})"

    @property
    def is_image(self):
        return self.mime_type.startswith("image/") or self.original_name.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")
        )

    @property
    def is_pdf(self):
        return self.mime_type == "application/pdf" or self.original_name.lower().endswith(".pdf")

    @property
    def is_video(self):
        return self.mime_type.startswith("video/") or self.original_name.lower().endswith(
            (".mp4", ".mov", ".avi", ".mkv")
        )

    @property
    def readable_size(self):
        size = self.size_bytes
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"


class TicketEvent(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=60)
    from_status = models.CharField(max_length=50, blank=True)
    to_status = models.CharField(max_length=50, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.ticket.number} - {self.action} at {self.created_at:%Y-%m-%d %H:%M}"


class TicketShareLink(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="share_links")
    ticket_file = models.ForeignKey(TicketFile, null=True, blank=True, on_delete=models.CASCADE, related_name="share_links")
    folder_path = models.CharField(max_length=500, blank=True)
    url = models.CharField(max_length=1000)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Share link for {self.ticket.number} (expires: {self.expires_at})"

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        return timezone.now() > self.expires_at

