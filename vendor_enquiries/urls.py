from django.urls import path

from . import views

app_name = "vendor_enquiries"

urlpatterns = [
    path("", views.enquiry_list, name="list"),
    path("new/", views.enquiry_create, name="create"),
    path("access/", views.team_access, name="access"),
    path("<int:pk>/", views.enquiry_detail, name="detail"),
    path("<int:pk>/edit/", views.enquiry_edit, name="edit"),
    path("<int:pk>/delete/", views.enquiry_delete, name="delete"),
    path("<int:pk>/revise/", views.enquiry_revise, name="revise"),
    path("<int:pk>/files/<int:file_id>/", views.enquiry_file, name="file"),
    path("<int:pk>/remarks/", views.remark_add, name="remark_add"),
    path("<int:pk>/remarks/<int:remark_id>/reply/", views.remark_reply, name="remark_reply"),
    path("<int:pk>/send-to-pricing/", views.send_to_pricing, name="send_to_pricing"),
]
