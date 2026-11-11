"""
Forms for Tickets and Projects management.
"""
from django import forms
from django.contrib.auth import get_user_model
from .models import Ticket, TicketFile

User = get_user_model()

TAILWIND_INPUT = (
    "w-full px-3 py-2 text-sm bg-white dark:bg-slate-800 border border-slate-300 "
    "dark:border-slate-700 rounded-lg text-slate-800 dark:text-slate-100 "
    "focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all"
)

TAILWIND_SELECT = (
    "w-full px-3 py-2 text-sm bg-white dark:bg-slate-800 border border-slate-300 "
    "dark:border-slate-700 rounded-lg text-slate-800 dark:text-slate-100 "
    "focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all"
)

TAILWIND_CHECKBOX = (
    "h-4 w-4 text-blue-600 focus:ring-blue-500 border-slate-300 dark:border-slate-700 "
    "rounded dark:bg-slate-800 transition"
)


class TicketCreateForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = [
            "type",
            "title",
            "description",
            "priority",
            "parts_purchased",
            "job_no",
            "enquiry_number",
            "quote_number",
            "customer_name",
            "zoho_customer_id",
            "salesperson_name",
            "client_contact_name",
            "client_phone",
            "client_email",
            "site_address",
            "warranty_start",
            "warranty_end",
            "parent_job",
            "assigned_to",
        ]
        widgets = {
            "type": forms.Select(attrs={"class": TAILWIND_SELECT}),
            "title": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "e.g. Rotary Valve Overhaul / Maintenance"}),
            "description": forms.Textarea(attrs={"class": TAILWIND_INPUT, "rows": 3, "placeholder": "Scope of work, problems reported, site notes..."}),
            "priority": forms.Select(attrs={"class": TAILWIND_SELECT}),
            "parts_purchased": forms.CheckboxInput(attrs={"class": TAILWIND_CHECKBOX}),
            "job_no": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "e.g. 1921"}),
            "enquiry_number": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "e.g. ENQ/2026/09/2708"}),
            "quote_number": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "e.g. 26000245R1"}),
            "customer_name": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "Customer / Company Name"}),
            "zoho_customer_id": forms.HiddenInput(),
            "salesperson_name": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "e.g. Umapathi / Esthie"}),
            "client_contact_name": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "Site Contact Name"}),
            "client_phone": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "Phone / Mobile"}),
            "client_email": forms.EmailInput(attrs={"class": TAILWIND_INPUT, "placeholder": "contact@example.com"}),
            "site_address": forms.Textarea(attrs={"class": TAILWIND_INPUT, "rows": 2, "placeholder": "Delivery / Plant Site Address"}),
            "warranty_start": forms.DateInput(attrs={"class": TAILWIND_INPUT, "type": "date"}),
            "warranty_end": forms.DateInput(attrs={"class": TAILWIND_INPUT, "type": "date"}),
            "parent_job": forms.Select(attrs={"class": TAILWIND_SELECT}),
            "assigned_to": forms.Select(attrs={"class": TAILWIND_SELECT}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Limit parent_job choices to PROJECT jobs only
        self.fields["parent_job"].queryset = Ticket.objects.filter(
            type=Ticket.TYPE_PROJECT, is_deleted=False
        ).order_by("-created_at")
        self.fields["parent_job"].required = False
        self.fields["assigned_to"].queryset = User.objects.filter(is_active=True).order_by("first_name", "username")
        self.fields["assigned_to"].required = False


class TicketUpdateForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = [
            "title",
            "description",
            "priority",
            "parts_purchased",
            "assigned_to",
            "customer_name",
            "salesperson_name",
            "client_contact_name",
            "client_phone",
            "client_email",
            "site_address",
            "warranty_start",
            "warranty_end",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"class": TAILWIND_INPUT}),
            "description": forms.Textarea(attrs={"class": TAILWIND_INPUT, "rows": 3}),
            "priority": forms.Select(attrs={"class": TAILWIND_SELECT}),
            "parts_purchased": forms.CheckboxInput(attrs={"class": TAILWIND_CHECKBOX}),
            "assigned_to": forms.Select(attrs={"class": TAILWIND_SELECT}),
            "customer_name": forms.TextInput(attrs={"class": TAILWIND_INPUT}),
            "salesperson_name": forms.TextInput(attrs={"class": TAILWIND_INPUT}),
            "client_contact_name": forms.TextInput(attrs={"class": TAILWIND_INPUT}),
            "client_phone": forms.TextInput(attrs={"class": TAILWIND_INPUT}),
            "client_email": forms.EmailInput(attrs={"class": TAILWIND_INPUT}),
            "site_address": forms.Textarea(attrs={"class": TAILWIND_INPUT, "rows": 2}),
            "warranty_start": forms.DateInput(attrs={"class": TAILWIND_INPUT, "type": "date"}),
            "warranty_end": forms.DateInput(attrs={"class": TAILWIND_INPUT, "type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = User.objects.filter(is_active=True).order_by("first_name", "username")
        self.fields["assigned_to"].required = False

