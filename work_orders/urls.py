from django.urls import path

from . import views

app_name = "work_orders"

urlpatterns = [
    path("", views.wo_list, name="list"),
    path("new/", views.wo_create, name="create"),
    path("access/", views.team_access, name="access"),
    path("<int:pk>/", views.wo_detail, name="detail"),
    path("<int:pk>/edit/", views.wo_edit, name="edit"),
    path("<int:pk>/pdf/", views.wo_pdf, name="pdf"),
    path("<int:pk>/submit/", views.wo_submit, name="submit"),
    path("<int:pk>/approve/", views.wo_approve, name="approve"),
    path("<int:pk>/send-back/", views.wo_send_back, name="send_back"),
    path("<int:pk>/delete/", views.wo_delete, name="delete"),
    path("<int:pk>/revise/", views.wo_revise, name="revise"),
    path("<int:pk>/status/", views.wo_status, name="status"),
    path("<int:pk>/checklist/<int:item_id>/", views.wo_tick, name="tick"),
    path("<int:pk>/progress/", views.wo_progress, name="progress"),
    path("<int:pk>/files/<int:att_id>/", views.attachment_file, name="file"),
    path("<int:pk>/files/<int:att_id>/update/", views.attachment_update, name="file_update"),
]
