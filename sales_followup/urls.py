from django.urls import path

from . import views

app_name = "sales_followup"

urlpatterns = [
    path("", views.followup_list, name="list"),
    path("monitor/", views.monitor, name="monitor"),
    path("access/", views.team_access, name="access"),
    path("<int:pk>/", views.followup_detail, name="detail"),
    path("entries/<int:entry_id>/edit/", views.entry_edit, name="entry_edit"),
    path("entries/<int:entry_id>/delete/", views.entry_delete, name="entry_delete"),
]
