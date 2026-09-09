from django.urls import path
from . import views

app_name = "drawings"

urlpatterns = [
    path("", views.drawing_list, name="drawing_list"),
    path("create/", views.drawing_create, name="drawing_create"),
    path("<int:pk>/", views.drawing_detail, name="drawing_detail"),
    path("<int:pk>/edit/", views.drawing_edit, name="drawing_edit"),
    path("<int:pk>/delete/", views.drawing_delete, name="drawing_delete"),
    path("<int:drawing_pk>/revisions/create/", views.revision_create, name="revision_create"),
    path("revisions/<int:revision_pk>/delete/", views.revision_delete, name="revision_delete"),
    path("revisions/<int:revision_pk>/pdf/download/", views.download_watermarked_pdf, name="download_watermarked_pdf"),
    path("revisions/<int:revision_pk>/pdf/view/", views.view_watermarked_pdf, name="view_watermarked_pdf"),
    path("revisions/<int:revision_pk>/native/download/", views.download_native_file, name="download_native_file"),
    path("access/grant/", views.grant_access, name="grant_access_global"),
    path("<int:drawing_pk>/access/grant/", views.grant_access, name="grant_access_drawing"),
    path("access/<int:access_pk>/revoke/", views.revoke_access, name="revoke_access"),
    path("access/global/", views.global_access_management, name="global_access_management"),
]
