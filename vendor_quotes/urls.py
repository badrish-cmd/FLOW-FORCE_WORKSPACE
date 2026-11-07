from django.urls import path

from . import views

app_name = "vendor_quotes"

urlpatterns = [
    path("", views.quote_list, name="quote_list"),
    path("new/", views.quote_create, name="quote_create"),
    path("enquiries/<int:pk>/", views.enquiry_detail, name="enquiry_detail"),
    path("quotes/<int:pk>/", views.quote_detail, name="quote_detail"),
    path("quotes/<int:pk>/file/", views.quote_file, name="quote_file"),
    path("quotes/<int:pk>/revise/", views.quote_revise, name="quote_revise"),
    path("revisions/<int:pk>/save/", views.revision_save, name="revision_save"),
    path("revisions/<int:pk>/submit/", views.revision_submit, name="revision_submit"),
    path("revisions/<int:pk>/approve/", views.revision_approve, name="revision_approve"),
    path("revisions/<int:pk>/send-back/", views.revision_send_back, name="revision_send_back"),
    path("revisions/<int:pk>/reject/", views.revision_reject, name="revision_reject"),
    path("revisions/<int:pk>/retry/", views.revision_retry, name="revision_retry"),
    path("revisions/<int:pk>/excel/", views.revision_excel, name="revision_excel"),
    path("vendors/", views.vendor_list, name="vendor_list"),
    path("vendors/<int:pk>/", views.vendor_detail, name="vendor_detail"),
    path("master-sheets/<int:pk>/", views.master_sheet_detail, name="master_sheet_detail"),
    path("master-files/<int:pk>/download/", views.master_file_download, name="master_file_download"),
    path("zoho/", views.zoho_settings, name="zoho_settings"),
    path("zoho/connect/", views.zoho_connect, name="zoho_connect"),
    path("zoho/callback/", views.zoho_callback, name="zoho_callback"),
    path("zoho/disconnect/", views.zoho_disconnect, name="zoho_disconnect"),
    path("zoho/customers/", views.zoho_customer_search, name="zoho_customer_search"),
]
