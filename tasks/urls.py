from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TaskViewSet, TaskCommentViewSet, ActivityLogViewSet, NotificationViewSet, reports_view, task_detail_view

router = DefaultRouter()
router.register("tasks", TaskViewSet, basename="task")
router.register("comments", TaskCommentViewSet, basename="comment")
router.register("activities", ActivityLogViewSet, basename="activity")
router.register("notifications", NotificationViewSet, basename="notification")

app_name = "tasks"

urlpatterns = [
    path("api/", include(router.urls)),
    path("reports/", reports_view, name="reports"),
    path("<int:task_id>/detail/", task_detail_view, name="task_detail"),
]
