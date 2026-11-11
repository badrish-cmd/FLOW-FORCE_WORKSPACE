from django.contrib import admin
from django.urls import path
from django.urls import include
from tasks.views import notification_mark_read_view
from django.contrib.staticfiles.storage import staticfiles_storage
from django.views.generic.base import RedirectView

urlpatterns = [

    path(
        "favicon.ico",
        RedirectView.as_view(url=staticfiles_storage.url("images/favicon.ico"), permanent=True),
        name="favicon"
    ),

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
        "drawings/",
        include(
            "drawing_library.urls",
            namespace="drawings"
        )
    ),

    path(
        "notifications/<int:notification_id>/read/",
        notification_mark_read_view,
        name="notification_mark_read"
    ),

    path(
        "announcements/",
        include("tasks.announcement_urls")
    ),

    path(
        "vendor-quotes/",
        include("vendor_quotes.urls")
    ),

    path(
        "tickets/",
        include("tickets.urls")
    ),

]


from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

