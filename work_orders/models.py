"""Work Orders issued to company teams for a PID (LIST PID - PT FLOW FORCE INDONESIA)."""
import os

from django.conf import settings
from django.db import models
from django.utils import timezone

STAGES = [
    (1, "Sales Order / Customer PO"),
    (2, "Engineering and Work Order Release"),
    (3, "Procurement and Fabrication"),
    (4, "Factory Acceptance Testing (FAT)"),
    (5, "Packing and Delivery"),
    (6, "Site Installation / Supervision"),
    (7, "Pre-Commissioning and Site Acceptance Testing (SAT)"),
    (8, "Commissioning and Performance Trial"),
    (9, "Customer Handover"),
    (10, "Project Closure and Warranty"),
]
STAGE_NAMES = dict(STAGES)

STAGE_CHECKLISTS = {
    1: ["Customer PO received", "Commercial scope confirmed", "Technical scope / clarifications closed", "Sales Order created"],
    2: ["Drawings prepared", "BOM prepared", "Customer approval of drawings", "Production instructions released"],
    3: ["Vendor POs issued", "Material received", "Fabrication / manufacturing done", "Assembly done", "Quality checks done"],
    4: ["Equipment inspection", "Test run", "Customer witness (if required)", "FAT report and release"],
    5: ["Packing done", "Packing list prepared", "Dispatch / transporter arranged", "Delivery note issued", "Customer receipt signed"],
    6: ["Mobilisation", "Mechanical installation", "Electrical installation", "Site report"],
    7: ["Safety checks", "Functional testing", "Acceptance verification (SAT report)"],
    8: ["Start-up", "Process testing", "Operational / performance verification"],
    9: ["Training given", "Manuals and documents handed over", "Signed acceptance"],
    10: ["Final invoice issued", "Payment received", "Outstanding items closed", "Warranty period started"],
}

# PDF sections chosen with checkboxes on each work order
SECTIONS = [
    ("invoice_address", "Invoice address"),
    ("delivery_address", "Delivery address"),
    ("order_date", "Order date"),
    ("required_by", "Required by"),
    ("customer_po", "Customer PO No."),
    ("quotation_no", "Quotation / Enquiry No."),
    ("sales_order", "Sales Order No."),
    ("delivery_method", "Delivery method"),
    ("team", "Team & assigned to"),
    ("items", "Item table"),
    ("prices", "Prices (rate, discount, amount, totals)"),
    ("stages", "Stage checklist"),
    ("instructions", "Scope & instructions"),
    ("notes", "Notes"),
    ("images", "Images"),
    ("signatures", "Prepared / Approved / Received by"),
]
SECTION_KEYS = [k for k, _ in SECTIONS]

TEAM_DEFAULTS = {
    "ENG": ["required_by", "customer_po", "quotation_no", "sales_order", "team", "items", "stages", "instructions",
            "notes", "images", "signatures"],
    "WH": ["delivery_address", "required_by", "customer_po", "sales_order", "delivery_method", "team", "items",
           "instructions", "notes", "images", "signatures"],
    "PRC": ["required_by", "customer_po", "quotation_no", "sales_order", "team", "items", "stages", "instructions",
            "notes", "signatures"],
    "ADM": ["invoice_address", "delivery_address", "order_date", "required_by", "customer_po", "quotation_no",
            "sales_order", "delivery_method", "team", "items", "instructions", "notes", "signatures"],
}
TEAM_DEFAULT_STAGES = {"ENG": [2], "WH": [5], "PRC": [3], "ADM": [1, 10]}


class Team(models.Model):
    code = models.CharField(max_length=10, unique=True, help_text="Used in the WO number, e.g. ENG")
    name = models.CharField(max_length=100)
    default_sections = models.JSONField(default=list, blank=True)
    default_stages = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class WorkOrderAccess(models.Model):
    """Admins and Super Admins always have full access. Assigned employees always see their own WOs."""
    LEVEL_VIEWER = "VIEWER"
    LEVEL_CREATOR = "CREATOR"
    LEVEL_CHOICES = [
        (LEVEL_VIEWER, "Viewer - sees all work orders"),
        (LEVEL_CREATOR, "Creator - creates work orders and submits them to GM"),
    ]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="work_order_access")
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class WorkOrderAccessChange(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    old_level = models.CharField(max_length=20, blank=True)
    new_level = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class NumberCounter(models.Model):
    """Last count per year + PID + team (WO-26-1932-ENG-01)."""
    year = models.PositiveIntegerField()
    pid = models.CharField(max_length=40)
    team_code = models.CharField(max_length=10)
    last = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("year", "pid", "team_code")]


class WorkOrder(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_IN_REVIEW = "IN_REVIEW"
    STATUS_SENT_BACK = "SENT_BACK"
    STATUS_ISSUED = "ISSUED"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_DONE = "DONE"
    STATUS_CLOSED = "CLOSED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_REVISED = "REVISED"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_IN_REVIEW, "Waiting for approval"),
        (STATUS_SENT_BACK, "Sent back"),
        (STATUS_ISSUED, "Approved & issued"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_DONE, "Done"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_REVISED, "Replaced by a revision"),
    ]
    EDITABLE = (STATUS_DRAFT, STATUS_SENT_BACK)
    ACTIVE = (STATUS_ISSUED, STATUS_IN_PROGRESS)

    number = models.CharField(max_length=60, unique=True, null=True, blank=True)
    base_number = models.CharField(max_length=60, blank=True, db_index=True)
    revision = models.PositiveIntegerField(default=0)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)

    # From LIST PID (internal work orders have pid "INT" and a typed reference)
    pid = models.CharField(max_length=40, db_index=True)
    is_internal = models.BooleanField(default=False)
    reference = models.CharField(max_length=255, blank=True, help_text="Internal work order: what / where it is for")
    pid_table = models.ForeignKey("tables.Table", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    pid_row_ids = models.JSONField(default=list, blank=True)
    customer_name = models.CharField(max_length=255, blank=True)
    invoice_address = models.TextField(blank=True)
    delivery_address = models.TextField(blank=True)
    order_date = models.DateField(null=True, blank=True)
    required_by = models.DateField(null=True, blank=True)
    customer_po = models.CharField(max_length=120, blank=True)
    quotation_no = models.CharField(max_length=255, blank=True)
    sales_order_no = models.CharField(max_length=120, blank=True)
    job_no = models.CharField(max_length=60, blank=True)
    delivery_method = models.CharField(max_length=120, blank=True)

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="work_orders")
    assignees = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="assigned_work_orders", blank=True)
    title = models.CharField(max_length=255, blank=True, help_text="Short summary of the job")
    instructions = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    stages = models.JSONField(default=list, blank=True)
    sections = models.JSONField(default=list, blank=True)
    ppn_percent = models.DecimalField(max_digits=5, decimal_places=2, default=11)
    currency = models.CharField(max_length=5, default="IDR")

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    gm_comment = models.TextField(blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    pdf = models.FileField(upload_to="work_orders/pdf/%Y/%m/", blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.display_number

    @property
    def display_number(self):
        return self.number or f"DRAFT-{self.pk or 'new'} ({self.pid_label} {self.team.code if self.team_id else ''})".strip()

    @property
    def pid_label(self):
        return f"Internal - {self.reference}" if self.is_internal else f"PID {self.pid}"

    @property
    def is_editable(self):
        return self.status in self.EDITABLE

    @property
    def is_overdue(self):
        return bool(self.required_by and self.status in self.ACTIVE and self.required_by < timezone.localdate())

    def has(self, section):
        return section in (self.sections or [])

    @property
    def stage_names(self):
        return [(s, STAGE_NAMES.get(s, f"Stage {s}")) for s in sorted(self.stages or [])]


class WorkOrderLine(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    description = models.TextField()
    qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=30, blank=True)
    rate = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    source_row_id = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]

    @property
    def amount(self):
        if self.rate is None or self.qty is None:
            return None
        return self.rate * self.qty - (self.discount or 0)


class ChecklistItem(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="checklist")
    stage = models.PositiveIntegerField()
    text = models.CharField(max_length=255)
    position = models.PositiveIntegerField(default=0)
    done = models.BooleanField(default=False)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["stage", "position", "id"]


def _attachment_path(instance, filename):
    now = timezone.now()
    return f"work_orders/files/{now:%Y/%m}/{os.path.basename(filename)}"


class Attachment(models.Model):
    IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=_attachment_path)
    original_name = models.CharField(max_length=255, blank=True)
    caption = models.CharField(max_length=255, blank=True)
    include_in_pdf = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    by_assignee = models.BooleanField(default=False, help_text="Added by the team while working on it")

    class Meta:
        ordering = ["uploaded_at", "id"]

    @property
    def filename(self):
        return self.original_name or os.path.basename(self.file.name)

    @property
    def is_image(self):
        return os.path.splitext(self.file.name)[1].lower() in self.IMAGE_EXT


class Note(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="progress_notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Event(models.Model):
    work_order = models.ForeignKey(WorkOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    label = models.CharField(max_length=120, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=40)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
