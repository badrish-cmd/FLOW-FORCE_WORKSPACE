from django import forms

from .models import MasterSheet, Vendor

INPUT = "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-2 focus:ring-blue-100"


class QuoteCreateForm(forms.Form):
    START_STANDARD = "standard"
    START_PREVIOUS = "previous"

    enquiry_number = forms.CharField(max_length=100, label="Enquiry no.",
                                     widget=forms.TextInput(attrs={"class": INPUT, "placeholder": "ENQ/2026/11/500", "list": "enquiry-options"}))
    customer_name = forms.CharField(max_length=255, label="Customer",
                                    widget=forms.TextInput(attrs={"class": INPUT, "placeholder": "PT. ..."}))
    vendor = forms.ModelChoiceField(queryset=Vendor.objects.filter(is_active=True), widget=forms.Select(attrs={"class": INPUT}))
    vendor_quote_no = forms.CharField(max_length=100, required=False, label="Vendor quote no.",
                                      widget=forms.TextInput(attrs={"class": INPUT, "placeholder": "e.g. 20263557"}))
    quote_file = forms.FileField(required=False, label="Vendor quote (PDF or Excel)",
                                 widget=forms.ClearableFileInput(attrs={"class": "block w-full text-sm", "accept": ".pdf,.xlsx,.xls,.png,.jpg,.jpeg"}))
    start = forms.ChoiceField(choices=[(START_STANDARD, "Standard price template"),
                                       (START_PREVIOUS, "Copy a previous sheet of this vendor")],
                              initial=START_STANDARD, widget=forms.RadioSelect)
    previous_sheet = forms.ModelChoiceField(queryset=MasterSheet.objects.none(), required=False,
                                            widget=forms.Select(attrs={"class": INPUT}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["previous_sheet"].queryset = MasterSheet.objects.select_related("vendor").order_by("vendor__name", "-position")
        self.fields["previous_sheet"].label_from_instance = (
            lambda s: f"{s.vendor.code or s.vendor.name} · {s.enquiry_no or s.sheet_name}"
                      f"{' · ' + s.customer_name if s.customer_name else ''}")

    def clean(self):
        data = super().clean()
        if data.get("start") == self.START_PREVIOUS:
            sheet = data.get("previous_sheet")
            if not sheet:
                self.add_error("previous_sheet", "Pick the sheet to copy.")
            elif data.get("vendor") and sheet.vendor_id != data["vendor"].id:
                self.add_error("previous_sheet", "That sheet belongs to another vendor.")
        return data


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = ["name", "code", "currency", "default_discount", "default_exchange_rate",
                  "default_margin_factor", "default_duty_factor", "default_freight_factor", "is_active", "notes"]
        labels = {
            "default_discount": "Default discount (0.15 = 15%)",
            "default_exchange_rate": "Exchange rate (IDR per 1 unit)",
            "default_margin_factor": "Margin factor (cost ÷ this)",
            "default_duty_factor": "Duty factor (0.9 = 10% duty)",
            "default_freight_factor": "Freight factor",
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2, "class": INPUT}),
            "is_active": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, f in self.fields.items():
            if name not in ("is_active", "notes"):
                f.widget.attrs.setdefault("class", INPUT)


class MasterUploadForm(forms.Form):
    file = forms.FileField(label="Master workbook (.xlsx)",
                           widget=forms.ClearableFileInput(attrs={"class": "block w-full text-sm", "accept": ".xlsx,.xlsm"}))

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".xlsx", ".xlsm")):
            raise forms.ValidationError("Please upload an .xlsx workbook.")
        return f
