import os
import re
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.files.storage import FileSystemStorage


class OverwriteStorage(FileSystemStorage):
    """
    Storage that overwrites existing files instead of appending random suffixes,
    guaranteeing strict adherence to the required naming pattern.
    """
    def get_available_name(self, name, max_length=None):
        if self.exists(name):
            self.delete(name)
        return name


drawing_storage = OverwriteStorage()


def sanitize_filename_part(val):
    """Sanitize string components for filesystem safe filenames."""
    if not val:
        return "UNKNOWN"
    # Replace whitespace and common separators with underscores
    s = re.sub(r'[\s/\\|:?*"<>]+', '_', str(val).strip())
    # Strip leading/trailing underscores or dots
    s = s.strip('._')
    return s or "UNKNOWN"


def drawing_pdf_upload_path(instance, filename):
    """
    Automatically rename uploaded PDF to strictly follow:
    [CustomerName]_[PID]_[DrawingName]_[Rev#].pdf
    When multiple drawings are saved under one PID with different names,
    each drawing receives its own unique, standardized filename.
    """
    customer = sanitize_filename_part(instance.drawing.customer_name if instance.drawing else "CUSTOMER")
    pid = sanitize_filename_part(instance.drawing.pid_reference if instance.drawing else "PID")
    dwg_name = sanitize_filename_part(instance.drawing.drawing_name if instance.drawing else "")
    rev = sanitize_filename_part(instance.revision_number or "0")
    if dwg_name:
        new_filename = f"{customer}_{pid}_{dwg_name}_{rev}.pdf"
    else:
        new_filename = f"{customer}_{pid}_{rev}.pdf"
    return os.path.join("drawings", "pdf", new_filename)


def drawing_native_upload_path(instance, filename):
    """
    Store native working files (.dwt, .slddrw, .dwg, etc.)
    under drawings/native/
    """
    base, ext = os.path.splitext(filename)
    clean_base = sanitize_filename_part(base)
    rev = sanitize_filename_part(instance.revision_number or "0")
    safe_name = f"{clean_base}_rev{rev}{ext.lower()}"
    return os.path.join("drawings", "native", safe_name)


class EngineeringDrawing(models.Model):
    FORMAT_CHOICES = [
        ("HAND_SKETCH", "Hand Sketch"),
        ("ZWCAD", "ZWCAD"),
        ("SOLIDWORKS", "SolidWorks"),
        ("AUTOCAD", "AutoCAD"),
        ("OTHER", "Other"),
    ]

    COMPANY_CHOICES = [
        ("PT FLOW FORCE INDONESIA", "PT FLOW FORCE INDONESIA"),
        ("PT FLOW FORCE ENGINEERING", "PT FLOW FORCE ENGINEERING"),
    ]

    # Project Metadata Header
    customer_name = models.CharField(max_length=255, verbose_name="Customer Name")
    enquiry_number = models.CharField(max_length=100, blank=True, null=True, verbose_name="Enquiry Number")
    po_number = models.CharField(max_length=100, blank=True, null=True, verbose_name="PO Number")
    pid_reference = models.CharField(max_length=150, verbose_name="P&ID Reference")
    drawing_name = models.CharField(max_length=255, verbose_name="Drawing Name")
    base_drawing_number = models.CharField(max_length=100, unique=True, verbose_name="Base Drawing Number")
    active_revision_number = models.CharField(max_length=50, default="0", verbose_name="Active Revision Number")
    drafter_name = models.CharField(max_length=150, verbose_name="Drafter Name")
    format = models.CharField(max_length=50, choices=FORMAT_CHOICES, default="ZWCAD", verbose_name="Format")
    watermark_company = models.CharField(
        max_length=100,
        choices=COMPANY_CHOICES,
        default="PT FLOW FORCE INDONESIA",
        verbose_name="Watermark Company"
    )

    description = models.TextField(blank=True, null=True, verbose_name="Description / Notes")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_drawings"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Engineering Drawing"
        verbose_name_plural = "Engineering Drawings"

    def __str__(self):
        return f"{self.base_drawing_number} - {self.drawing_name} ({self.customer_name})"

    @property
    def latest_revision(self):
        return self.revisions.order_by("-created_at").first()

    def sync_active_revision(self):
        latest = self.latest_revision
        if latest and latest.revision_number:
            self.active_revision_number = latest.revision_number
            self.save(update_fields=["active_revision_number", "updated_at"])


class DrawingRevision(models.Model):
    drawing = models.ForeignKey(
        EngineeringDrawing,
        on_delete=models.CASCADE,
        related_name="revisions"
    )
    revision_number = models.CharField(max_length=50, verbose_name="Revision #")
    stage_change_description = models.TextField(verbose_name="Stage Change Description")
    
    # Native working file (.dwt, .slddrw, .dwg, etc.)
    native_file = models.FileField(
        upload_to=drawing_native_upload_path,
        blank=True,
        null=True,
        verbose_name="Native File Attachment"
    )
    
    # Exported PDF automatically renamed to [CustomerName]_[PID]_[Rev#].pdf
    pdf_file = models.FileField(
        upload_to=drawing_pdf_upload_path,
        storage=drawing_storage,
        blank=True,
        null=True,
        verbose_name="Exported PDF File"
    )

    drafter_name = models.CharField(max_length=150, verbose_name="Drafter Name")
    revision_date = models.DateField(default=timezone.now, verbose_name="Revision Date")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drawing_revisions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision_date", "-created_at"]
        verbose_name = "Drawing Revision"
        verbose_name_plural = "Drawing Revisions"

    def __str__(self):
        return f"{self.drawing.base_drawing_number} - Rev {self.revision_number}"

    def expected_pdf_filename(self):
        customer = sanitize_filename_part(self.drawing.customer_name if self.drawing else "CUSTOMER")
        pid = sanitize_filename_part(self.drawing.pid_reference if self.drawing else "PID")
        dwg_name = sanitize_filename_part(self.drawing.drawing_name if self.drawing else "")
        rev = sanitize_filename_part(self.revision_number or "0")
        if dwg_name:
            return f"{customer}_{pid}_{dwg_name}_{rev}.pdf"
        return f"{customer}_{pid}_{rev}.pdf"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Keep drawing active revision number up to date
        if self.drawing:
            self.drawing.sync_active_revision()


class DrawingAccess(models.Model):
    ACCESS_LEVEL_CHOICES = [
        ("VIEW", "View Only"),
        ("EDIT", "Edit & Upload Revisions"),
    ]

    # If drawing is null, access applies to the entire Drawing Library
    drawing = models.ForeignKey(
        EngineeringDrawing,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="access_grants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="drawing_accesses"
    )
    department = models.ForeignKey(
        "employee_management.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drawing_accesses"
    )
    access_level = models.CharField(
        max_length=20,
        choices=ACCESS_LEVEL_CHOICES,
        default="VIEW"
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_drawing_accesses"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Drawing Access Permission"
        verbose_name_plural = "Drawing Access Permissions"

    def __str__(self):
        scope = self.drawing.base_drawing_number if self.drawing else "Global Library"
        return f"{scope} -> {self.user.email} ({self.access_level})"


class DrawingActivityLog(models.Model):
    ACTION_CHOICES = [
        ("VIEW_DRAWING", "Viewed Drawing"),
        ("VIEW_PDF", "Previewed Watermarked PDF"),
        ("DOWNLOAD_PDF", "Downloaded Watermarked PDF"),
        ("DOWNLOAD_NATIVE", "Downloaded Native CAD File"),
        ("CREATE_DRAWING", "Created Drawing Project"),
        ("UPDATE_DRAWING", "Updated Drawing Metadata"),
        ("DELETE_DRAWING", "Deleted Drawing Project"),
        ("UPLOAD_REVISION", "Uploaded New Revision"),
        ("DELETE_REVISION", "Deleted Revision"),
        ("GRANT_ACCESS", "Granted Access Clearance"),
        ("REVOKE_ACCESS", "Revoked Access Clearance"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drawing_activities"
    )
    drawing = models.ForeignKey(
        EngineeringDrawing,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_logs"
    )
    revision = models.ForeignKey(
        DrawingRevision,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_logs"
    )
    drawing_number = models.CharField(max_length=100, blank=True, null=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    description = models.TextField(blank=True, null=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Drawing Activity Log"
        verbose_name_plural = "Drawing Activity Logs"

    def __str__(self):
        user_name = self.user.full_name if self.user else "Anonymous"
        return f"{user_name} - {self.get_action_display()} ({self.created_at.strftime('%Y-%m-%d %H:%M')})"


def log_drawing_activity(user, action, drawing=None, revision=None, description="", request=None):
    """Utility function to log every employee activity inside the drawing library."""
    ip = None
    if request:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0].strip()
        else:
            ip = request.META.get("REMOTE_ADDR")

    dwg_number = None
    if drawing:
        dwg_number = drawing.base_drawing_number
    elif revision and revision.drawing:
        dwg_number = revision.drawing.base_drawing_number

    return DrawingActivityLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        drawing=drawing,
        revision=revision,
        drawing_number=dwg_number,
        action=action,
        description=description,
        ip_address=ip,
    )

