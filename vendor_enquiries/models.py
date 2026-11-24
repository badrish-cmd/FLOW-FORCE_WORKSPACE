"""Vendor Enquiry Tracker: one row per enquiry sent to one vendor (no prices here)."""
import os

from django.conf import settings
from django.db import models
from django.utils import timezone


def _upload_path(instance, filename):
    now = timezone.now()
    return f"vendor_enquiries/{now:%Y/%m}/{os.path.basename(filename)}"


class TrackerAccess(models.Model):
    """Who may use the tracker. Admins and Super Admins always can and need no row here."""
    LEVEL_USER = "USER"
    LEVEL_MANAGER = "MANAGER"
    LEVEL_CHOICES = [
        (LEVEL_USER, "User - own enquiries only"),
        (LEVEL_MANAGER, "Manager - all enquiries, remarks, send to price calculation"),
    ]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vendor_enquiry_access")
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - {self.get_level_display()}"


class AccessChange(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    old_level = models.CharField(max_length=20, blank=True)
    new_level = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class VendorEnquiry(models.Model):
    STATUS_SENT = "ENQUIRY_SENT"
    STATUS_PENDING = "PENDING_REPLY"
    STATUS_QUOTE_GOT = "QUOTE_GOT"
    STATUS_CHOICES = [
        (STATUS_SENT, "Enquiry Sent"),
        (STATUS_PENDING, "Pending Reply from Vendor"),
        (STATUS_QUOTE_GOT, "Quote Got"),
    ]

    company = models.CharField(max_length=5, choices=[("FFI", "FFI - PT. Flow Force Indonesia"),
                                                      ("FFE", "FFE - PT. Flow Force Engineering")], default="FFI")
    enquiry_no = models.CharField(max_length=100, db_index=True)
    customer_name = models.CharField(max_length=255, blank=True)
    vendor_name = models.CharField(max_length=200)
    vendor = models.ForeignKey("vendor_quotes.Vendor", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="tracker_enquiries", help_text="Set when the name matches a Vendor Quotes vendor")
    initial_enquiry = models.TextField(blank=True)
    sent_on = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SENT, db_index=True)
    vendor_quote_no = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="vendor_enquiries")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    quote_got_at = models.DateTimeField(null=True, blank=True)

    # Price calculation link (set by a Manager)
    vendor_quote = models.ForeignKey("vendor_quotes.VendorQuote", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="tracker_enquiries")
    sent_file = models.ForeignKey("QuoteFile", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sent_to_pricing_at = models.DateTimeField(null=True, blank=True)
    sent_to_pricing_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name="+")

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.enquiry_no} - {self.vendor_name}"

    @property
    def latest_file(self):
        return self.files.order_by("-revision").first()

    @property
    def label(self):
        return f"{self.enquiry_no} · {self.vendor_name}"


class QuoteFile(models.Model):
    enquiry = models.ForeignKey(VendorEnquiry, on_delete=models.CASCADE, related_name="files")
    revision = models.PositiveIntegerField(default=0, help_text="0 = first quote, 1 = Revision 1, ...")
    file = models.FileField(upload_to=_upload_path)
    vendor_quote_no = models.CharField(max_length=100, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        unique_together = [("enquiry", "revision")]

    @property
    def label(self):
        return "Original quote" if self.revision == 0 else f"Revision {self.revision}"

    @property
    def filename(self):
        return os.path.basename(self.file.name)


class Remark(models.Model):
    enquiry = models.ForeignKey(VendorEnquiry, on_delete=models.CASCADE, related_name="remarks")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class TrackerLog(models.Model):
    """History, kept even after a row is deleted."""
    enquiry = models.ForeignKey(VendorEnquiry, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs")
    label = models.CharField(max_length=320, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=40)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
