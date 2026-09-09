import os
from django import forms
from django.utils import timezone
from .models import EngineeringDrawing, DrawingRevision, DrawingAccess
from auth_app.models import EmployeeUser
from employee_management.models import Department


class EngineeringDrawingForm(forms.ModelForm):
    # Initial revision files and notes
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
        label="Stage Notes / Description",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Initial Draft / Released for Review", "class": "form-control"})
    )
    initial_native_file = forms.FileField(
        required=False,
        label="CAD Source File (ZWCAD, SolidWorks, DWG, etc.)",
        widget=forms.FileInput(attrs={"class": "form-control"})
    )
    initial_pdf_file = forms.FileField(
        required=False,
        label="PDF Drawing File",
        widget=forms.FileInput(attrs={"class": "form-control", "accept": ".pdf"})
    )

    class Meta:
        model = EngineeringDrawing
        fields = [
            # Starting Stage: Project Information
            "customer_name",
            "project_name",
            "pid_reference",
            "drafter_name",
            # Drawing Details
            "base_drawing_number",
            "drawing_name",
            "description",
            "format",
            "parent",
            "drawing_type",
        ]
        widgets = {
            "customer_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Chevron Indonesia", "required": True}),
            "project_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Fuel Gas Conditioning Skid", "required": True}),
            "pid_reference": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. PID-B-102", "list": "existingPidsList", "required": True}),
            "drafter_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Budi Santoso", "required": True}),
            "base_drawing_number": forms.TextInput(attrs={"class": "form-control font-mono", "placeholder": "e.g. FF-DWG-00109", "required": True}),
            "drawing_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Piping General Arrangement", "required": True}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Drawing scope, specifications, or notes..."}),
            "format": forms.Select(attrs={"class": "form-select"}),
            "parent": forms.Select(attrs={"class": "form-select"}),
            "drawing_type": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = "-- None (Level 1 Parent / Master Drawing) --"
        self.fields["parent"].queryset = EngineeringDrawing.objects.all().order_by("base_drawing_number")
        self.fields["drawing_type"].required = False
        if not self.initial.get("drawing_type"):
            self.initial["drawing_type"] = "MASTER"

    def clean(self):
        cleaned_data = super().clean()
        parent = cleaned_data.get("parent")
        drawing_type = cleaned_data.get("drawing_type")
        if not drawing_type:
            if not parent:
                cleaned_data["drawing_type"] = "MASTER"
            elif parent.level == 1:
                cleaned_data["drawing_type"] = "SUB_ASSEMBLY"
            else:
                cleaned_data["drawing_type"] = "DETAIL_PART"
        return cleaned_data


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
        self.fields["revision_date"].required = False
        if not self.initial.get("revision_date"):
            self.initial["revision_date"] = timezone.now().date()

    def clean_revision_date(self):
        d = self.cleaned_data.get("revision_date")
        if not d:
            return timezone.now().date()
        return d

    def save(self, commit=True):
        instance = super().save(commit=False)
        pdf = self.cleaned_data.get("pdf_file")
        if pdf and hasattr(pdf, "name"):
            instance.original_pdf_filename = os.path.basename(pdf.name)
        native = self.cleaned_data.get("native_file")
        if native and hasattr(native, "name"):
            instance.original_native_filename = os.path.basename(native.name)
        if commit:
            instance.save()
        return instance


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

