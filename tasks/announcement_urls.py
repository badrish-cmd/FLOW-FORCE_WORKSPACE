from django.urls import path
from .announcement_views import (
    announcements_list_view,
    announcement_acknowledge_view,
    admin_announcements_view,
    admin_announcement_create_view,
    admin_announcement_edit_view,
    admin_announcement_toggle_publish_view,
    admin_announcement_delete_view,
)

urlpatterns = [
    path("", announcements_list_view, name="announcements_list"),
    path("<int:announcement_id>/acknowledge/", announcement_acknowledge_view, name="announcement_acknowledge"),
    path("manage/", admin_announcements_view, name="announcements_manage"),
    path("create/", admin_announcement_create_view, name="announcement_create"),
    path("<int:announcement_id>/edit/", admin_announcement_edit_view, name="announcement_edit"),
    path("<int:announcement_id>/toggle-publish/", admin_announcement_toggle_publish_view, name="announcement_toggle_publish"),
    path("<int:announcement_id>/delete/", admin_announcement_delete_view, name="announcement_delete"),
]
