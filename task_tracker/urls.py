from django.urls import path

app_name = 'task_tracker'

from .views import task_create
from .views import task_detail
from .views import task_edit
from .views import task_status_update
from .views import notification_mark_read
from .views import comment_create, attachment_upload
from .views import tracker_spreadsheet
from .views import tracker_kanban
from .views import tracker_calendar
from .views import tracker_timeline
from .views import bulk_tasks
from .views import save_filter
from .views import apply_saved_filter
from .views import export_tasks_csv
from .views import import_tasks_csv
from .views import my_tasks
from .views import overdue_tasks
from .views import tracker_column_create
from .views import tracker_column_delete
from .views import tracker_column_reorder
from .views import tracker_column_update
from .views import tracker_create
from .views import tracker_detail
from .views import tracker_dashboard
from .views import tracker_edit
from .views import tracker_delete
from .views import (
    ajax_cell_edit,
    ajax_add_row,
    ajax_duplicate_row,
    ajax_delete_row,
    ajax_restore_row,
    ajax_add_column,
    ajax_edit_column,
    tracker_share,
    task_mark_review,
    task_review_decide,
    comment_pin_toggle,
)


urlpatterns = [
	path("", tracker_dashboard, name="tracker_dashboard"),
	path("my-tasks/", my_tasks, name="my_tasks"),
	path("overdue/", overdue_tasks, name="overdue_tasks"),
	path("create/", tracker_create, name="tracker_create"),
	path("<int:tracker_id>/", tracker_detail, name="tracker_detail"),
	path("<int:tracker_id>/spreadsheet/", tracker_spreadsheet, name="tracker_spreadsheet"),
	path("<int:tracker_id>/kanban/", tracker_kanban, name="tracker_kanban"),
	path("<int:tracker_id>/calendar/", tracker_calendar, name="tracker_calendar"),
	path("<int:tracker_id>/timeline/", tracker_timeline, name="tracker_timeline"),
	path("<int:tracker_id>/edit/", tracker_edit, name="tracker_edit"),
	path("<int:tracker_id>/delete/", tracker_delete, name="tracker_delete"),
	path("<int:tracker_id>/columns/add/", tracker_column_create, name="tracker_column_create"),
	path("<int:tracker_id>/columns/<int:column_id>/edit/", tracker_column_update, name="tracker_column_update"),
	path("<int:tracker_id>/columns/<int:column_id>/delete/", tracker_column_delete, name="tracker_column_delete"),
	path("<int:tracker_id>/columns/reorder/", tracker_column_reorder, name="tracker_column_reorder"),
	path("<int:tracker_id>/tasks/create/", task_create, name="task_create"),
	path("<int:tracker_id>/tasks/<int:task_id>/", task_detail, name="task_detail"),
	path("<int:tracker_id>/tasks/<int:task_id>/edit/", task_edit, name="task_edit"),
	path("<int:tracker_id>/tasks/<int:task_id>/status/", task_status_update, name="task_status_update"),
	path("notifications/<int:notification_id>/read/", notification_mark_read, name="notification_mark_read"),
	path("<int:tracker_id>/tasks/<int:task_id>/comments/add/", comment_create, name="task_comment_create"),
	path("<int:tracker_id>/tasks/<int:task_id>/attachments/add/", attachment_upload, name="task_attachment_upload"),
	path("<int:tracker_id>/bulk/", bulk_tasks, name="task_bulk_actions"),
	path("<int:tracker_id>/filters/save/", save_filter, name="task_filter_save"),
	path("<int:tracker_id>/filters/<int:filter_id>/apply/", apply_saved_filter, name="task_filter_apply"),
	path("<int:tracker_id>/export/csv/", export_tasks_csv, name="task_export_csv"),
	path("<int:tracker_id>/import/csv/", import_tasks_csv, name="task_import_csv"),
	# Spreadsheet AJAX Endpoints
	path("<int:tracker_id>/ajax/cells/<int:task_id>/edit/", ajax_cell_edit, name="ajax_cell_edit"),
	path("<int:tracker_id>/ajax/rows/add/", ajax_add_row, name="ajax_add_row"),
	path("<int:tracker_id>/ajax/rows/<int:task_id>/duplicate/", ajax_duplicate_row, name="ajax_duplicate_row"),
	path("<int:tracker_id>/ajax/rows/<int:task_id>/delete/", ajax_delete_row, name="ajax_delete_row"),
	path("<int:tracker_id>/ajax/rows/<int:task_id>/restore/", ajax_restore_row, name="ajax_restore_row"),
	path("<int:tracker_id>/ajax/columns/add/", ajax_add_column, name="ajax_add_column"),
	path("<int:tracker_id>/ajax/columns/<int:column_id>/edit/", ajax_edit_column, name="ajax_edit_column"),
	path("<int:tracker_id>/share/", tracker_share, name="tracker_share"),
	path("<int:tracker_id>/tasks/<int:task_id>/review/submit/", task_mark_review, name="task_mark_review"),
	path("<int:tracker_id>/tasks/<int:task_id>/review/decide/", task_review_decide, name="task_review_decide"),
	path("<int:tracker_id>/tasks/<int:task_id>/comments/<int:comment_id>/pin/", comment_pin_toggle, name="comment_pin_toggle"),
]