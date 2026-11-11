"""
URL patterns for tickets app.
"""
from django.urls import path
from . import views

app_name = "tickets"

urlpatterns = [
    path("", views.ticket_list, name="ticket_list"),
    path("create/", views.ticket_create, name="ticket_create"),
    path("<int:ticket_id>/", views.ticket_detail, name="ticket_detail"),
    path("<int:ticket_id>/edit/", views.ticket_update, name="ticket_update"),
    path("<int:ticket_id>/status/", views.ticket_status_change, name="ticket_status_change"),
    path("<int:ticket_id>/note/", views.ticket_add_note, name="ticket_add_note"),
    path("<int:ticket_id>/upload-chunk/", views.ticket_upload_chunk, name="ticket_upload_chunk"),
    path("<int:ticket_id>/add-from-link/", views.ticket_add_from_link, name="ticket_add_from_link"),
    path("<int:ticket_id>/share-link/", views.ticket_create_share_link, name="ticket_create_share_link"),
    path("<int:ticket_id>/files/<int:file_id>/download/", views.ticket_file_download, name="ticket_file_download"),
    path("<int:ticket_id>/files/<int:file_id>/delete/", views.ticket_file_delete, name="ticket_file_delete"),
    path("<int:ticket_id>/delete/", views.ticket_delete, name="ticket_delete"),
    path("api/zoho-lookup/", views.zoho_lookup_api, name="zoho_lookup_api"),
]

