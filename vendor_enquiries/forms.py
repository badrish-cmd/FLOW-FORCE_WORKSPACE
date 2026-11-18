from django import forms

from .models import VendorEnquiry

INPUT = "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-2 focus:ring-blue-100"


class EnquiryForm(forms.Form):
    enquiry_no = forms.CharField(max_length=100, label="Enquiry No.",
                                 widget=forms.TextInput(attrs={"class": INPUT, "placeholder": "ENQ/2026/11/500"}))
    customer_name = forms.CharField(max_length=255, required=False, label="Customer",
                                    widget=forms.TextInput(attrs={"class": INPUT, "placeholder": "PT. ..."}))
    vendor_name = forms.CharField(max_length=200, label="Vendor",
                                  widget=forms.TextInput(attrs={"class": INPUT, "list": "vendor-options",
                                                                "placeholder": "Pick from the list or type a new vendor",
                                                                "autocomplete": "off"}))
    initial_enquiry = forms.CharField(required=False, label="Initial Enquiry",
                                      widget=forms.Textarea(attrs={"class": INPUT, "rows": 5,
                                                                   "placeholder": "What is asked from the vendor - items, models, quantities ..."}))
    sent_on = forms.DateField(label="Date sent", widget=forms.DateInput(attrs={"class": INPUT, "type": "date"}))
    status = forms.ChoiceField(choices=VendorEnquiry.STATUS_CHOICES, widget=forms.Select(attrs={"class": INPUT}))
    vendor_quote_no = forms.CharField(max_length=100, required=False, label="Vendor Quote No.",
                                      widget=forms.TextInput(attrs={"class": INPUT, "placeholder": "e.g. 20263557"}))
    quote_file = forms.FileField(required=False, label="Vendor quote (PDF, Excel or image)",
                                 widget=forms.ClearableFileInput(attrs={"class": "block w-full text-sm",
                                                                        "accept": ".pdf,.xlsx,.xls,.png,.jpg,.jpeg"}))
