"""
Vendor quote -> customer quotation automation.

Flow: a vendor quote is uploaded against an enquiry, its lines are priced in an
Excel-like sheet (standard BOS template, everything editable), the GM approves,
then the approved sheet is written into the vendor's master workbook and a draft
quotation is created in Zoho Books. Customer revisions create R1, R2, ...
"""
import os
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


def _dated(prefix, filename):
    return f"vendor_quotes/{prefix}/{timezone.now():%Y/%m}/{filename}"


def master_upload_path(instance, filename):
    return _dated("masters", filename)


def vendor_file_upload_path(instance, filename):
    return _dated("vendor_files", filename)


def approved_upload_path(instance, filename):
    return _dated("approved", filename)


class Vendor(models.Model):
    name = models.CharField(max_length=200, unique=True)
    code = models.CharField(max_length=50, blank=True, help_text="Short name, e.g. BOS")
    currency = models.CharField(max_length=10, default="EUR")
    # Defaults that pre-fill the standard price template (all editable per quote)
    default_discount = models.DecimalField(max_digits=6, decimal_places=4, default=0, help_text="0.15 = 15%")
    default_exchange_rate = models.DecimalField(max_digits=14, decimal_places=4, default=0, help_text="IDR per 1 unit of vendor currency")
    default_margin_factor = models.DecimalField(max_digits=6, decimal_places=4, default=0.8, help_text="Cost is divided by this, e.g. 0.8")
    default_duty_factor = models.DecimalField(max_digits=6, decimal_places=4, default=0.9, help_text="0.9 = duty 10%")
    default_freight_factor = models.DecimalField(max_digits=6, decimal_places=4, default=1)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def current_master(self):
        return self.master_files.order_by("-created_at").first()


class VendorMasterFile(models.Model):
    """One version of a vendor's master workbook. The newest one is current."""
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="master_files")
    file = models.FileField(upload_to=master_upload_path)
    note = models.CharField(max_length=255, blank=True)
    sheet_count = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.vendor} master ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def filename(self):
        return os.path.basename(self.file.name)


class MasterSheet(models.Model):
    """A past quote: one sheet split out of an imported vendor master workbook."""
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="master_sheets")
    source = models.ForeignKey(VendorMasterFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="sheets")
    sheet_name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)
    enquiry_no = models.CharField(max_length=100, blank=True, db_index=True)
    customer_name = models.CharField(max_length=255, blank=True)
    vendor_quote_no = models.CharField(max_length=100, blank=True)
    quotation_no = models.CharField(max_length=100, blank=True)
    snapshot = models.JSONField(default=dict)
    details_missing = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vendor", "-position"]

    def __str__(self):
        return f"{self.vendor} / {self.sheet_name}"


class Enquiry(models.Model):
    number = models.CharField(max_length=100, unique=True, help_text="e.g. ENQ/2026/10/484")
    customer_name = models.CharField(max_length=255)
    zoho_customer_id = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "enquiries"

    def __str__(self):
        return self.number


class VendorQuote(models.Model):
    AI_NOT_STARTED = "NOT_STARTED"
    AI_RUNNING = "RUNNING"
    AI_DONE = "DONE"
    AI_FAILED = "FAILED"
    AI_STATUS_CHOICES = [
        (AI_NOT_STARTED, "Not started"),
        (AI_RUNNING, "Running"),
        (AI_DONE, "Done"),
        (AI_FAILED, "Failed"),
    ]

    enquiry = models.ForeignKey(Enquiry, on_delete=models.CASCADE, related_name="quotes")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="quotes")
    vendor_quote_no = models.CharField(max_length=100, blank=True)
    quote_file = models.FileField(upload_to=vendor_file_upload_path, blank=True)
    currency = models.CharField(max_length=10, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Phase 6: AI quote extraction
    ai_status = models.CharField(max_length=20, choices=AI_STATUS_CHOICES, default=AI_NOT_STARTED, db_index=True)
    ai_result = models.JSONField(default=dict, blank=True)
    ai_error = models.TextField(blank=True)
    ai_ran_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.enquiry} / {self.vendor} {self.vendor_quote_no}".strip()

    @property
    def latest_revision(self):
        return self.revisions.order_by("-number").first()

    @property
    def quote_file_name(self):
        if not self.quote_file:
            return ""
        return os.path.basename(self.quote_file.name)

    @property
    def quote_file_is_pdf(self):
        return bool(self.quote_file) and self.quote_file.name.lower().endswith(".pdf")


class QuoteRevision(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_IN_REVIEW = "IN_REVIEW"
    STATUS_SENT_BACK = "SENT_BACK"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_VOID = "VOID"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_IN_REVIEW, "In review"),
        (STATUS_SENT_BACK, "Sent back"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_VOID, "Void"),
    ]
    OUTPUT_NOT_STARTED = "NOT_STARTED"
    OUTPUT_PENDING = "PENDING"
    OUTPUT_WAITING = "WAITING_CONNECTION"
    OUTPUT_DONE = "DONE"
    OUTPUT_FAILED = "FAILED"
    OUTPUT_CHOICES = [
        (OUTPUT_NOT_STARTED, "Not started"),
        (OUTPUT_PENDING, "Running"),
        (OUTPUT_WAITING, "Waiting for Zoho connection"),
        (OUTPUT_DONE, "Done"),
        (OUTPUT_FAILED, "Failed"),
    ]

    quote = models.ForeignKey(VendorQuote, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField(default=0, help_text="0 = original, 1 = R1, ...")
    snapshot = models.JSONField(default=dict, help_text="Univer workbook snapshot (values + formulas)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.TextField(blank=True)

    # Output 1: Excel price table + vendor master
    excel_status = models.CharField(max_length=30, choices=OUTPUT_CHOICES, default=OUTPUT_NOT_STARTED)
    excel_file = models.FileField(upload_to=approved_upload_path, blank=True)
    master_sheet_name = models.CharField(max_length=100, blank=True)
    excel_error = models.TextField(blank=True)

    # Output 2: Zoho Books draft quotation
    zoho_status = models.CharField(max_length=30, choices=OUTPUT_CHOICES, default=OUTPUT_NOT_STARTED)
    zoho_estimate_id = models.CharField(max_length=50, blank=True)
    zoho_estimate_number = models.CharField(max_length=60, blank=True)
    zoho_error = models.TextField(blank=True)

    # Quotation details for Zoho draft quotation
    quote_subject = models.CharField(max_length=255, blank=True)
    validity_days = models.PositiveIntegerField(default=30)
    salesperson_name = models.CharField(max_length=100, blank=True)
    salesperson_id = models.CharField(max_length=50, blank=True)
    delivery_time = models.CharField(max_length=255, blank=True, help_text='e.g. "10 to 12 weeks from PO"')
    payment_terms = models.CharField(
        max_length=255,
        default="50% Down payment and Balance 50% Payment after delivery of goods"
    )
    customer_notes = models.TextField(default="Looking forward for your business.")
    extra_terms = models.TextField(
        default="All Delivery time quoted are after receipt of Order and subject to availability"
    )
    line_details = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-line customizations (quote description, commodity code, etc.) keyed by line number"
    )

    class Meta:
        ordering = ["quote", "number"]
        unique_together = [("quote", "number")]

    def __str__(self):
        return f"{self.quote} {self.label}"

    @property
    def label(self):
        return "Original" if self.number == 0 else f"R{self.number}"

    @property
    def is_editable(self):
        return self.status in (self.STATUS_DRAFT, self.STATUS_SENT_BACK)

    @property
    def expiry_date(self):
        from datetime import timedelta
        base_date = self.created_at.date() if self.created_at else timezone.localdate()
        return base_date + timedelta(days=self.validity_days or 30)


class QuoteChange(models.Model):
    """Audit trail: one changed cell in a revision's sheet."""
    revision = models.ForeignKey(QuoteRevision, on_delete=models.CASCADE, related_name="changes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sheet = models.CharField(max_length=100, blank=True)
    cell = models.CharField(max_length=20)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class QuoteEvent(models.Model):
    """Timeline of what happened to a quote (submitted, approved, Zoho created, ...)."""
    quote = models.ForeignKey(VendorQuote, on_delete=models.CASCADE, related_name="events")
    revision = models.ForeignKey(QuoteRevision, null=True, blank=True, on_delete=models.CASCADE, related_name="events")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ZohoConnection(models.Model):
    """Single row holding the Zoho Books OAuth connection (set up via 'Connect Zoho')."""
    refresh_token = models.TextField(blank=True)
    access_token = models.TextField(blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    api_domain = models.CharField(max_length=200, blank=True)
    connected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    connected_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_connected(self):
        return bool(self.refresh_token)


class AIRequestLog(models.Model):
    """Log of every AI request made to providers (e.g. Gemini) with token usage and cost tracking."""
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="ai_request_logs", db_index=True)
    quote = models.ForeignKey(VendorQuote, null=True, blank=True, on_delete=models.SET_NULL, related_name="ai_request_logs")
    feature = models.CharField(max_length=50, default="quote_reading")
    provider = models.CharField(max_length=50, default="gemini")
    model = models.CharField(max_length=100, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_type = models.CharField(max_length=50, blank=True)
    file_size_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)
    error_message = models.TextField(blank=True)
    duration_ms = models.IntegerField(default=0)
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    lines_extracted = models.IntegerField(default=0)
    warnings_count = models.IntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["user"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.model} ({self.status})"

    @property
    def duration_seconds(self):
        return round(self.duration_ms / 1000.0, 2)


class AIBudgetNotification(models.Model):
    """Tracks monthly threshold notifications (80%, 100%) sent to approvers to prevent duplicates."""
    year_month = models.CharField(max_length=7, db_index=True)  # e.g. "2026-11"
    threshold = models.IntegerField()  # 80 or 100
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("year_month", "threshold")]

    def __str__(self):
        return f"{self.year_month} - {self.threshold}% alert at {self.sent_at}"
