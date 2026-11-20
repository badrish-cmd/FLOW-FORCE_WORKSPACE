"""Sales Follow-up: record and monitor follow-ups on approved quotes (no prices, no mails sent)."""
from django.conf import settings
from django.db import models


class FollowupAccess(models.Model):
    """Admins and Super Admins always have full access and need no row here."""
    LEVEL_USER = "USER"
    LEVEL_MANAGER = "MANAGER"
    LEVEL_CHOICES = [
        (LEVEL_USER, "User - records follow-ups"),
        (LEVEL_MANAGER, "Manager - monitoring, can correct any entry"),
    ]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sales_followup_access")
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class FollowupAccessChange(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    old_level = models.CharField(max_length=20, blank=True)
    new_level = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class SalesFollowup(models.Model):
    STATUS_WAITING = "WAITING"
    STATUS_FOLLOWED_UP = "FOLLOWED_UP"
    STATUS_REVIEWING = "REVIEWING"
    STATUS_WON = "WON"
    STATUS_LOST = "LOST"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_CHOICES = [
        (STATUS_WAITING, "Waiting for update"),
        (STATUS_FOLLOWED_UP, "Followed up"),
        (STATUS_REVIEWING, "Customer reviewing"),
        (STATUS_WON, "PO received / Won"),
        (STATUS_LOST, "Lost"),
        (STATUS_CANCELLED, "Cancelled (quote voided)"),
    ]
    ENTRY_STATUSES = STATUS_CHOICES[:5]
    CLOSED = (STATUS_WON, STATUS_LOST, STATUS_CANCELLED)

    quote = models.OneToOneField("vendor_quotes.VendorQuote", on_delete=models.CASCADE, related_name="sales_followup")
    revision = models.ForeignKey("vendor_quotes.QuoteRevision", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    enquiry_no = models.CharField(max_length=100, db_index=True)
    customer_name = models.CharField(max_length=255, blank=True)
    vendor_name = models.CharField(max_length=200, blank=True)
    revision_label = models.CharField(max_length=20, blank=True)
    zoho_number = models.CharField(max_length=60, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)


    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_WAITING, db_index=True)
    next_followup_date = models.DateField(null=True, blank=True, db_index=True)
    last_followup_date = models.DateField(null=True, blank=True)
    followup_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-approved_at"]

    def __str__(self):
        return f"{self.enquiry_no} {self.zoho_number}"

    @property
    def is_open(self):
        return self.status not in self.CLOSED


class FollowupEntry(models.Model):
    CHANNEL_CHOICES = [("EMAIL", "Email"), ("WHATSAPP", "WhatsApp"), ("CALL", "Call"), ("MEETING", "Meeting"), ("OTHER", "Other")]

    followup = models.ForeignKey(SalesFollowup, on_delete=models.CASCADE, related_name="entries")
    date = models.DateField()
    contacted = models.CharField(max_length=255, help_text="Who she followed up with")
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default="EMAIL")
    update_text = models.TextField("Update received", blank=True)
    status = models.CharField(max_length=20, choices=SalesFollowup.ENTRY_STATUSES)
    next_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date", "-created_at"]


class FollowupLog(models.Model):
    followup = models.ForeignKey(SalesFollowup, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs")
    label = models.CharField(max_length=200, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
