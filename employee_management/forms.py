from django import forms

from auth_app.models import EmployeeUser
from .models import Department


class EmployeeForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "autocomplete": "new-password",
            }
        ),
        help_text="Leave blank to keep the existing password.",
    )

    class Meta:
        model = EmployeeUser
        fields = (
            "full_name",
            "email",
            "role",
            "status",
            "is_active",
        )
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "role": forms.Select(attrs={"class": "form-select"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        self.current_user = kwargs.pop("current_user", None)
        self.can_edit_role = kwargs.pop("can_edit_role", True)
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.fields["password"].required = False
        else:
            self.fields["password"].required = True
            self.fields["password"].help_text = "Required for new employees."

        if (
            self.current_user
            and self.current_user.role == "ADMIN"
        ):
            self.fields["role"].choices = [
                choice
                for choice in EmployeeUser.ROLE_CHOICES
                if choice[0] not in ("SUPER_ADMIN",)
            ]

        if (
            self.current_user
            and self.current_user.role == "DEPARTMENT_ADMIN"
        ):
            self.fields["role"].choices = [
                ("EMPLOYEE", "Employee"),
            ]

        if not self.can_edit_role:
            self.fields["role"].disabled = True
            self.fields["role"].help_text = "You cannot edit this employee role."

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        existing = EmployeeUser.objects.filter(email=email)

        if self.instance and self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)

        if existing.exists():
            raise forms.ValidationError("An employee with this email already exists.")

        return email

    def clean_role(self):
        role = self.cleaned_data["role"]

        if (
            self.current_user
            and self.current_user.role == "ADMIN"
            and role == "SUPER_ADMIN"
        ):
            raise forms.ValidationError("Admins cannot assign Super Admin role.")

        return role

    def save(self, commit=True):
        employee = super().save(commit=False)
        password = self.cleaned_data.get("password")

        if password:
            employee.set_password(password)

        if commit:
            employee.save()
            self.save_m2m()

        return employee
