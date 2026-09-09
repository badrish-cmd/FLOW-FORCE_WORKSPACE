from django import forms
from django.utils import timezone
from .models import EngineeringDrawing, DrawingRevision, DrawingAccess
from auth_app.models import EmployeeUser
from employee_management.models import Department


class EngineeringDrawingForm(forms.ModelForm):
    # Optional fields for initial revision on creation
    initial_revision_number = forms.CharField(
        max_length=50,
        required=False,
        initial="0",
        label="Initial Revision #",
        widget=forms.TextInput(attrs={"placeholder": "e.g. 0 or R0", "class": "form-control"})
    )
    initial_stage_description = forms.CharField(
        max_length=255,
        required=False,
        initial="Initial Release",
        label="Stage Change Description",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Initial Draft / Released for Review", "class": "form-control"})
    )
    initial_native_file = forms.FileField(
        required=False,
        label="Native CAD File (.dwt, .slddrw, .dwg, etc.)",
        widget=forms.FileInput(attrs={"class": "form-control"})
    )
    initial_pdf_file = forms.FileField(
        required=False,
        label="Exported PDF File",
        widget=forms.FileInput(attrs={"class": "form-control", "accept": ".pdf"})
    )

    class Meta:
        model = EngineeringDrawing
        fields = [
            "customer_name",
            "enquiry_number",
            "po_number",
            "pid_reference",
            "drawing_name",
            "base_drawing_number",
            "drafter_name",
            "format",
            "watermark_company",
            "description",
        ]
        widgets = {
            "customer_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Chevron Indonesia"}),
            "enquiry_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. ENQ-2026-0042"}),
            "po_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. PO-88391"}),
            "pid_reference": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. PID-B-102", "list": "existingPidsList"}),
            "drawing_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Piping Isometric Skid A"}),
            "base_drawing_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. FF-DWG-00109"}),
            "drafter_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Budi Santoso"}),
            "format": forms.Select(attrs={"class": "form-select"}),
            "watermark_company": forms.Select(attrs={"class": "form-select"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Optional project specifications, tolerances, or notes..."}),
        }


class DrawingRevisionForm(forms.ModelForm):
    class Meta:
        model = DrawingRevision
        fields = [
            "revision_number",
            "stage_change_description",
            "native_file",
            "pdf_file",
            "drafter_name",
            "revision_date",
        ]
        widgets = {
            "revision_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. A, B, 01, R1"}),
            "stage_change_description": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Detailed description of stage or design change..."}),
            "native_file": forms.FileInput(attrs={"class": "form-control"}),
            "pdf_file": forms.FileInput(attrs={"class": "form-control", "accept": ".pdf"}),
            "drafter_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Drafter Name"}),
            "revision_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get("revision_date"):
            self.initial["revision_date"] = timezone.now().date()


class DrawingAccessGrantForm(forms.ModelForm):
    class Meta:
        model = DrawingAccess
        fields = [
            "user",
            "access_level",
        ]
        widgets = {
            "user": forms.Select(attrs={"class": "form-select"}),
            "access_level": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].required = True
        self.fields["user"].empty_label = "-- Choose Employee --"
        self.fields["user"].queryset = EmployeeUser.objects.filter(is_active=True).order_by("full_name")

