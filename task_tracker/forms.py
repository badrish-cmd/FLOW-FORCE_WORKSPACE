from __future__ import annotations

from django import forms

from auth_app.models import EmployeeUser

from .models import TASK_PRIORITY_CHOICES
from .models import TASK_STATUS_CHOICES
from .models import Tracker
from .models import TrackerColumn
from .models import TaskFilter
from .services import create_custom_column


class TrackerForm(forms.ModelForm):
    class Meta:
        model = Tracker
        fields = ("department", "name", "description", "is_active")
        widgets = {
            "department": forms.Select(attrs={"class": "form-select"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class TrackerColumnForm(forms.ModelForm):
    class Meta:
        model = TrackerColumn
        fields = ("label",)
        widgets = {
            "label": forms.TextInput(attrs={"class": "form-control", "placeholder": "Custom column name"}),
        }

    def __init__(self, *args, **kwargs):
        self.tracker = kwargs.pop("tracker", None)
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        label = self.cleaned_data["label"]
        if self.instance and self.instance.pk:
            self.instance.label = label.strip()
            if commit:
                self.instance.save(update_fields=["label", "updated_at"])
            return self.instance
        if not self.tracker:
            raise ValueError("Tracker is required for new columns.")
        return create_custom_column(self.tracker, label)


class TaskRowForm(forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    due_date = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    task_name = forms.CharField(widget=forms.TextInput(attrs={"class": "form-control"}))
    priority = forms.ChoiceField(
        choices=TASK_PRIORITY_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
        required=False,
    )
    assigned_to = forms.ModelChoiceField(
        queryset=EmployeeUser.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    assigned_by = forms.ModelChoiceField(
        queryset=EmployeeUser.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = forms.ChoiceField(
        choices=TASK_STATUS_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        self.tracker = kwargs.pop("tracker", None)
        self.request_user = kwargs.pop("request_user", None)
        self.instance = kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)

        employee_queryset = EmployeeUser.objects.all().order_by("full_name", "email")
        if self.tracker and self.tracker.department:
            employee_queryset = employee_queryset.filter(department=self.tracker.department)
        self.fields["assigned_to"].queryset = employee_queryset
        self.fields["assigned_by"].queryset = employee_queryset

        if self.request_user and self.request_user.role == "EMPLOYEE":
            self.fields["assigned_by"].initial = self.request_user
            self.fields["assigned_by"].disabled = True

        self.dynamic_columns = []
        if self.tracker:
            self.dynamic_columns = list(self.tracker.columns.filter(is_fixed=False).order_by("position", "id"))
            for column in self.dynamic_columns:
                field_name = f"column_{column.id}"
                self.fields[field_name] = forms.CharField(
                    label=column.label,
                    required=False,
                    widget=forms.TextInput(attrs={"class": "form-control"}),
                )
                if self.instance:
                    try:
                        cell = self.instance.cells.get(column=column)
                        self.fields[field_name].initial = cell.value
                    except Exception:
                        self.fields[field_name].initial = ""

        if self.instance:
            self.fields["date"].initial = self.instance.date
            self.fields["due_date"].initial = self.instance.due_date
            self.fields["task_name"].initial = self.instance.task_name
            self.fields["priority"].initial = self.instance.priority
            self.fields["assigned_to"].initial = self.instance.assigned_to
            self.fields["assigned_by"].initial = self.instance.assigned_by
            self.fields["status"].initial = self.instance.status

    def clean(self):
        cleaned = super().clean()
        assigned_to = cleaned.get("assigned_to")
        assigned_by = cleaned.get("assigned_by")
        if assigned_to and assigned_by and assigned_to.department != assigned_by.department:
            raise forms.ValidationError("Assigned By and Assigned To must belong to the same department tracker.")
        return cleaned

    def dynamic_values(self):
        return {
            key: value
            for key, value in self.cleaned_data.items()
            if key.startswith("column_")
        }


class CommentForm(forms.Form):
    content = forms.CharField(widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    internal = forms.BooleanField(required=False, widget=forms.CheckboxInput())
    parent_id = forms.IntegerField(required=False, widget=forms.HiddenInput())


class AttachmentForm(forms.Form):
    file = forms.FileField()


class TaskFilterForm(forms.Form):
    search = forms.CharField(required=False, widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Search tasks"}))
    department = forms.CharField(required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    employee = forms.ModelChoiceField(required=False, queryset=EmployeeUser.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    status = forms.ChoiceField(required=False, choices=[("", "All Statuses")] + list(TASK_STATUS_CHOICES), widget=forms.Select(attrs={"class": "form-select"}))
    priority = forms.ChoiceField(required=False, choices=[("", "All Priorities")] + list(TASK_PRIORITY_CHOICES), widget=forms.Select(attrs={"class": "form-select"}))
    due_date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    due_date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    overdue = forms.BooleanField(required=False)
    completed = forms.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        tracker = kwargs.pop("tracker", None)
        request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)

        employee_queryset = EmployeeUser.objects.all().order_by("full_name", "email")
        if tracker and tracker.department:
            employee_queryset = employee_queryset.filter(department=tracker.department)
        self.fields["employee"].queryset = employee_queryset
        if request_user and request_user.role == "EMPLOYEE":
            self.fields["employee"].initial = request_user


class TaskFilterSaveForm(forms.ModelForm):
    class Meta:
        model = TaskFilter
        fields = ("name",)
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Save filter name"}),
        }


class TaskImportForm(forms.Form):
    file = forms.FileField(widget=forms.ClearableFileInput(attrs={"class": "form-control"}))
