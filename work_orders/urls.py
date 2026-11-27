from django.urls import path

from . import project_views as pv
from . import views

app_name = "work_orders"

urlpatterns = [
    path("", views.wo_list, name="list"),
    path("new/", views.wo_create, name="create"),
    path("new-team/", views.legacy_create, name="create_team"),
    path("access/", views.team_access, name="access"),
    path("my-tasks/", pv.my_tasks, name="my_tasks"),
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
    path("<int:pk>/checklist/add/", views.checklist_add, name="checklist_add"),
    path("<int:pk>/checklist/<int:item_id>/edit/", views.checklist_edit, name="checklist_edit"),
    path("<int:pk>/files/<int:att_id>/", views.attachment_file, name="file"),
    path("<int:pk>/files/<int:att_id>/update/", views.attachment_update, name="file_update"),
    # single work order
    path("<int:pk>/stages/add/", pv.stage_add, name="stage_add"),
    path("<int:pk>/stages/<int:stage_id>/", pv.stage_update, name="stage_update"),
    path("<int:pk>/stages/<int:stage_id>/done/", pv.stage_done, name="stage_done"),
    path("<int:pk>/departments/add/", pv.dept_add, name="dept_add"),
    path("<int:pk>/departments/<int:dept_id>/", pv.dept_update, name="dept_update"),
    path("<int:pk>/departments/<int:dept_id>/items/add/", pv.item_add, name="item_add"),
    path("<int:pk>/items/<int:item_id>/", pv.item_update, name="item_update"),
    path("<int:pk>/items/<int:item_id>/tick/", pv.item_tick, name="item_tick"),
    path("<int:pk>/spec/", pv.spec_save, name="spec_save"),
    path("<int:pk>/spec/confirm/", pv.spec_confirm, name="spec_confirm"),
]
