from django.contrib import admin
from django.urls import path
from django.urls import include
from tasks.views import notification_mark_read_view

urlpatterns = [

    path(
        "admin/",
        admin.site.urls
    ),

    path(
        "",
        include(
            "auth_app.urls"
        )
    ),

    path(
        "employees/",
        include(
            "employee_management.urls"
        )
    ),

    path(
        "trackers/",
        include(
            "task_tracker.urls"
        )
    ),

    path(
        "tables/",
        include(
            "tables.urls"
        )
    ),

    path(
        "tasks/",
        include(
            "tasks.urls"
        )
    ),

    path(
        "notifications/<int:notification_id>/read/",
        notification_mark_read_view,
        name="notification_mark_read"
    ),

]
