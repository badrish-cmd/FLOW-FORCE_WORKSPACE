from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TableViewSet, ColumnViewSet, RowViewSet, TableAccessViewSet, ColumnAccessViewSet, table_spreadsheet_view, table_create_view, table_list_view, tables_analytics_dashboard, pid_dashboard_view

router = DefaultRouter()
router.register("tables", TableViewSet, basename="table")
router.register("columns", ColumnViewSet, basename="column")
router.register("rows", RowViewSet, basename="row")
router.register("table-access", TableAccessViewSet, basename="tableaccess")
router.register("column-access", ColumnAccessViewSet, basename="columnaccess")

app_name = "tables"

urlpatterns = [
    path("api/", include(router.urls)),
    path("", table_list_view, name="table_list"),
    path("pid-dashboard/", pid_dashboard_view, name="pid_dashboard"),
    path("analytics/", tables_analytics_dashboard, name="analytics_dashboard"),
    path("<int:table_id>/", table_spreadsheet_view, name="table_spreadsheet"),
    path("create/", table_create_view, name="table_create"),
]

