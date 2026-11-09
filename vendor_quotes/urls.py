from django.urls import path

from . import views

app_name = "vendor_quotes"

urlpatterns = [
    path("", views.quote_list, name="quote_list"),
    path("new/", views.quote_create, name="quote_create"),
    path("enquiries/<int:pk>/", views.enquiry_detail, name="enquiry_detail"),
    path("enquiries/<int:pk>/delete/", views.enquiry_delete, name="enquiry_delete"),
    path("quotes/<int:pk>/", views.quote_detail, name="quote_detail"),
    path("quotes/<int:pk>/delete/", views.quote_delete, name="quote_delete"),
    path("quotes/<int:pk>/file/", views.quote_file, name="quote_file"),
    path("quotes/<int:pk>/ai-read/", views.quote_ai_read, name="quote_ai_read"),
    path("quotes/<int:pk>/ai-status/", views.quote_ai_status, name="quote_ai_status"),
    path("quotes/<int:pk>/revise/", views.quote_revise, name="quote_revise"),
    path("revisions/<int:pk>/save/", views.revision_save, name="revision_save"),
    path("revisions/<int:pk>/submit/", views.revision_submit, name="revision_submit"),
    path("revisions/<int:pk>/approve/", views.revision_approve, name="revision_approve"),
    path("revisions/<int:pk>/send-back/", views.revision_send_back, name="revision_send_back"),
    path("revisions/<int:pk>/reject/", views.revision_reject, name="revision_reject"),
    path("revisions/<int:pk>/discard/", views.revision_discard, name="revision_discard"),
    path("revisions/<int:pk>/void/", views.revision_void, name="revision_void"),
    path("revisions/<int:pk>/retry/", views.revision_retry, name="revision_retry"),
    path("revisions/<int:pk>/excel/", views.revision_excel, name="revision_excel"),
    path("vendors/", views.vendor_list, name="vendor_list"),
    path("vendors/<int:pk>/", views.vendor_detail, name="vendor_detail"),
    path("vendors/<int:pk>/delete/", views.vendor_delete, name="vendor_delete"),
    path("vendors/<int:pk>/toggle-active/", views.vendor_toggle_active, name="vendor_toggle_active"),
    path("vendors/<int:pk>/master-sheets/bulk-delete/", views.master_sheets_bulk_delete, name="master_sheets_bulk_delete"),
    path("master-sheets/<int:pk>/", views.master_sheet_detail, name="master_sheet_detail"),
    path("master-sheets/<int:pk>/delete/", views.master_sheet_delete, name="master_sheet_delete"),
    path("master-files/<int:pk>/download/", views.master_file_download, name="master_file_download"),
    path("master-files/<int:pk>/delete/", views.master_file_delete, name="master_file_delete"),
    path("master-files/<int:pk>/restore/", views.master_file_restore, name="master_file_restore"),
    path("zoho/", views.zoho_settings, name="zoho_settings"),
    path("zoho/connect/", views.zoho_connect, name="zoho_connect"),
    path("zoho/callback/", views.zoho_callback, name="zoho_callback"),
    path("zoho/disconnect/", views.zoho_disconnect, name="zoho_disconnect"),
    path("zoho/customers/", views.zoho_customer_search, name="zoho_customer_search"),
    path("ai-usage/", views.ai_usage, name="ai_usage"),
    path("ai-usage/export/", views.ai_usage_export_csv, name="ai_usage_export"),
]
