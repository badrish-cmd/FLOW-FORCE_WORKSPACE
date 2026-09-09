import os
import io
from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, Http404, JsonResponse
from django.contrib import messages
from django.db.models import Q, Count
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone

from auth_app.models import EmployeeUser
from .models import (
    EngineeringDrawing,
    DrawingRevision,
    DrawingAccess,
    DrawingActivityLog,
    log_drawing_activity,
)
from .forms import EngineeringDrawingForm, DrawingRevisionForm, DrawingAccessGrantForm
from .permissions import (
    is_admin_or_superadmin,
    can_manage_drawing_access,
    has_library_access,
    get_accessible_drawings,
    has_drawing_access,
    drawing_library_access_required,
    admin_or_superadmin_required,
)
from .watermark import apply_company_watermark


@drawing_library_access_required
def drawing_list(request):
    user = request.user
    accessible_drawings = get_accessible_drawings(user)

    # Search & filters
    q = request.GET.get("q", "").strip()
    format_filter = request.GET.get("format", "").strip()
    company_filter = request.GET.get("company", "").strip()
    view_mode = request.GET.get("view", "pid")  # 'pid' or 'flat'

    drawings_qs = accessible_drawings
    if q:
        drawings_qs = drawings_qs.filter(
            Q(customer_name__icontains=q) |
            Q(pid_reference__icontains=q) |
            Q(base_drawing_number__icontains=q) |
            Q(drawing_name__icontains=q) |
            Q(enquiry_number__icontains=q) |
            Q(po_number__icontains=q) |
            Q(drafter_name__icontains=q)
        )
    if format_filter:
        drawings_qs = drawings_qs.filter(format=format_filter)
    if company_filter:
        drawings_qs = drawings_qs.filter(watermark_company=company_filter)

    drawings = drawings_qs.prefetch_related("revisions").select_related("created_by")

    # Group drawings under P&ID references (multiple drawings under one PID with different names)
    pid_groups = {}
    for d in drawings:
        pid = d.pid_reference.strip() if d.pid_reference else "UNASSIGNED"
        if pid not in pid_groups:
            pid_groups[pid] = {
                "pid_reference": pid,
                "customer_name": d.customer_name,
                "drawings": [],
                "total_revisions": 0,
            }
        pid_groups[pid]["drawings"].append(d)
        pid_groups[pid]["total_revisions"] += d.revisions.count()

    # Metrics
    total_drawings = accessible_drawings.count()
    total_pids = len(pid_groups)
    total_revisions = DrawingRevision.objects.filter(drawing__in=accessible_drawings).count()

    is_admin = is_admin_or_superadmin(user)

    context = {
        "drawings": drawings,
        "pid_groups": pid_groups.values(),
        "total_drawings": total_drawings,
        "total_pids": total_pids,
        "total_revisions": total_revisions,
        "view_mode": view_mode,
        "search_query": q,
        "format_filter": format_filter,
        "company_filter": company_filter,
        "format_choices": EngineeringDrawing.FORMAT_CHOICES,
        "company_choices": EngineeringDrawing.COMPANY_CHOICES,
        "is_admin": is_admin,
    }
    return render(request, "drawing_library/drawing_list.html", context)


@drawing_library_access_required
def drawing_detail(request, pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=pk)
    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to view this engineering drawing.")
        return redirect("drawings:drawing_list")

    # Log employee view activity
    log_drawing_activity(
        user=request.user,
        action="VIEW_DRAWING",
        drawing=drawing,
        description=f"Viewed project metadata and revision history for {drawing.base_drawing_number}",
        request=request
    )

    can_edit = has_drawing_access(request.user, drawing, required_level="EDIT") or is_admin_or_superadmin(request.user)
    is_admin = is_admin_or_superadmin(request.user)

    revisions = drawing.revisions.all().order_by("-revision_date", "-created_at")
    revision_form = DrawingRevisionForm(initial={"drafter_name": drawing.drafter_name})
    access_form = DrawingAccessGrantForm() if is_admin else None

    # Other drawings saved under this same P&ID reference
    other_pid_drawings = EngineeringDrawing.objects.filter(
        pid_reference=drawing.pid_reference
    ).exclude(pk=drawing.pk).prefetch_related("revisions")

    # Access grants for this drawing
    access_grants = drawing.access_grants.select_related("user", "granted_by") if is_admin else []
    
    # Recent activity logs for this drawing (visible to admin)
    recent_activities = drawing.activity_logs.select_related("user").order_by("-created_at")[:20] if is_admin else []

    context = {
        "drawing": drawing,
        "revisions": revisions,
        "other_pid_drawings": other_pid_drawings,
        "revision_form": revision_form,
        "access_form": access_form,
        "access_grants": access_grants,
        "recent_activities": recent_activities,
        "can_edit": can_edit,
        "is_admin": is_admin,
    }
    return render(request, "drawing_library/drawing_detail.html", context)


@drawing_library_access_required
def drawing_create(request):
    if not is_admin_or_superadmin(request.user):
        messages.error(request, "Only Admins and Super Admins can create new drawing projects.")
        return redirect("drawings:drawing_list")

    # Existing PIDs list for easy autocomplete / datalist
    existing_pids = list(
        EngineeringDrawing.objects.values_list("pid_reference", flat=True)
        .distinct()
        .order_by("pid_reference")
    )

    if request.method == "POST":
        form = EngineeringDrawingForm(request.POST, request.FILES)
        if form.is_valid():
            drawing = form.save(commit=False)
            drawing.created_by = request.user
            drawing.save()

            log_drawing_activity(
                user=request.user,
                action="CREATE_DRAWING",
                drawing=drawing,
                description=f"Created engineering drawing '{drawing.drawing_name}' ({drawing.base_drawing_number}) under P&ID {drawing.pid_reference}",
                request=request
            )

            # Handle optional initial revision if files or notes are provided
            init_rev = form.cleaned_data.get("initial_revision_number") or "0"
            init_desc = form.cleaned_data.get("initial_stage_description") or "Initial Release"
            init_native = form.cleaned_data.get("initial_native_file")
            init_pdf = form.cleaned_data.get("initial_pdf_file")

            if init_native or init_pdf:
                rev = DrawingRevision.objects.create(
                    drawing=drawing,
                    revision_number=init_rev,
                    stage_change_description=init_desc,
                    native_file=init_native,
                    pdf_file=init_pdf,
                    drafter_name=drawing.drafter_name,
                    created_by=request.user,
                )
                drawing.sync_active_revision()

                log_drawing_activity(
                    user=request.user,
                    action="UPLOAD_REVISION",
                    drawing=drawing,
                    revision=rev,
                    description=f"Uploaded initial revision Rev {rev.revision_number} for {drawing.drawing_name}",
                    request=request
                )

            messages.success(request, f"Drawing '{drawing.drawing_name}' ({drawing.base_drawing_number}) saved under P&ID '{drawing.pid_reference}'.")
            return redirect("drawings:drawing_detail", pk=drawing.pk)
    else:
        initial_data = {}
        if request.GET.get("pid"):
            initial_data["pid_reference"] = request.GET.get("pid")
        if request.GET.get("customer"):
            initial_data["customer_name"] = request.GET.get("customer")
        if request.GET.get("watermark"):
            initial_data["watermark_company"] = request.GET.get("watermark")
        form = EngineeringDrawingForm(initial=initial_data)

    return render(request, "drawing_library/drawing_form.html", {
        "form": form,
        "existing_pids": existing_pids,
        "title": "New Engineering Drawing Project",
        "is_create": True,
    })


@drawing_library_access_required
def drawing_edit(request, pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=pk)
    if not (is_admin_or_superadmin(request.user) or has_drawing_access(request.user, drawing, required_level="EDIT")):
        messages.error(request, "Access Denied: You do not have permission to edit this drawing.")
        return redirect("drawings:drawing_detail", pk=pk)

    if request.method == "POST":
        form = EngineeringDrawingForm(request.POST, instance=drawing)
        if form.is_valid():
            form.save()

            log_drawing_activity(
                user=request.user,
                action="UPDATE_DRAWING",
                drawing=drawing,
                description=f"Updated project metadata for {drawing.base_drawing_number}",
                request=request
            )

            messages.success(request, f"Drawing '{drawing.base_drawing_number}' updated successfully.")
            return redirect("drawings:drawing_detail", pk=drawing.pk)
    else:
        form = EngineeringDrawingForm(instance=drawing)

    return render(request, "drawing_library/drawing_form.html", {
        "form": form,
        "drawing": drawing,
        "title": f"Edit Drawing - {drawing.base_drawing_number}",
        "is_create": False,
    })


@admin_or_superadmin_required
def drawing_delete(request, pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=pk)
    if request.method == "POST":
        drawing_num = drawing.base_drawing_number
        log_drawing_activity(
            user=request.user,
            action="DELETE_DRAWING",
            drawing=drawing,
            description=f"Deleted drawing project {drawing_num} and all its revisions",
            request=request
        )
        drawing.delete()
        messages.success(request, f"Drawing '{drawing_num}' and its revisions have been deleted.")
        return redirect("drawings:drawing_list")
    return redirect("drawings:drawing_detail", pk=pk)


@drawing_library_access_required
def revision_create(request, drawing_pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=drawing_pk)
    if not (is_admin_or_superadmin(request.user) or has_drawing_access(request.user, drawing, required_level="EDIT")):
        messages.error(request, "You do not have permission to upload revisions to this drawing.")
        return redirect("drawings:drawing_detail", pk=drawing.pk)

    if request.method == "POST":
        form = DrawingRevisionForm(request.POST, request.FILES)
        if form.is_valid():
            revision = form.save(commit=False)
            revision.drawing = drawing
            revision.created_by = request.user
            revision.save()
            drawing.sync_active_revision()

            log_drawing_activity(
                user=request.user,
                action="UPLOAD_REVISION",
                drawing=drawing,
                revision=revision,
                description=f"Uploaded Rev {revision.revision_number}: {revision.stage_change_description[:80]}",
                request=request
            )

            messages.success(request, f"Revision {revision.revision_number} added successfully with standardized naming.")
            return redirect("drawings:drawing_detail", pk=drawing.pk)
        else:
            messages.error(request, "Please correct the errors in the revision upload form.")
            return redirect("drawings:drawing_detail", pk=drawing.pk)

    return redirect("drawings:drawing_detail", pk=drawing.pk)


@admin_or_superadmin_required
def revision_delete(request, revision_pk):
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing
    rev_num = revision.revision_number

    log_drawing_activity(
        user=request.user,
        action="DELETE_REVISION",
        drawing=drawing,
        description=f"Deleted Revision {rev_num} from drawing {drawing.base_drawing_number}",
        request=request
    )

    revision.delete()
    drawing.sync_active_revision()
    messages.success(request, f"Revision {rev_num} has been removed.")
    return redirect("drawings:drawing_detail", pk=drawing.pk)


@drawing_library_access_required
def download_watermarked_pdf(request, revision_pk):
    """
    Download the exported PDF with a light, semi-transparent watermark
    of the selected company name in the corner.
    """
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing

    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to download this PDF.")
        return redirect("drawings:drawing_list")

    if not revision.pdf_file or not os.path.exists(revision.pdf_file.path):
        raise Http404("PDF file attachment not found.")

    company_name = drawing.watermark_company or "PT FLOW FORCE INDONESIA"

    try:
        watermarked_io = apply_company_watermark(revision.pdf_file.path, company_name)
    except Exception as e:
        with open(revision.pdf_file.path, "rb") as f:
            pdf_data = f.read()
        watermarked_io = io.BytesIO(pdf_data)

    filename = revision.expected_pdf_filename()

    # Log employee download activity
    log_drawing_activity(
        user=request.user,
        action="DOWNLOAD_PDF",
        drawing=drawing,
        revision=revision,
        description=f"Downloaded watermarked PDF ({filename}) with {company_name} watermark stamp",
        request=request
    )

    response = HttpResponse(watermarked_io.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@drawing_library_access_required
def view_watermarked_pdf(request, revision_pk):
    """
    View the watermarked PDF directly in the browser (inline viewer/iframe).
    """
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing

    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to view this PDF.")
        return redirect("drawings:drawing_list")

    if not revision.pdf_file or not os.path.exists(revision.pdf_file.path):
        raise Http404("PDF file attachment not found.")

    company_name = drawing.watermark_company or "PT FLOW FORCE INDONESIA"

    try:
        watermarked_io = apply_company_watermark(revision.pdf_file.path, company_name)
    except Exception as e:
        with open(revision.pdf_file.path, "rb") as f:
            pdf_data = f.read()
        watermarked_io = io.BytesIO(pdf_data)

    filename = revision.expected_pdf_filename()

    # Log employee preview activity
    log_drawing_activity(
        user=request.user,
        action="VIEW_PDF",
        drawing=drawing,
        revision=revision,
        description=f"Previewed watermarked PDF for {drawing.base_drawing_number} Rev {revision.revision_number} in browser",
        request=request
    )

    response = HttpResponse(watermarked_io.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@drawing_library_access_required
def download_native_file(request, revision_pk):
    """
    Download the native CAD working file (.dwt, .slddrw, .dwg, etc.).
    """
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing

    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to download this native file.")
        return redirect("drawings:drawing_list")

    if not revision.native_file or not os.path.exists(revision.native_file.path):
        raise Http404("Native file attachment not found.")

    filename = os.path.basename(revision.native_file.name)

    # Log employee native CAD file download activity
    log_drawing_activity(
        user=request.user,
        action="DOWNLOAD_NATIVE",
        drawing=drawing,
        revision=revision,
        description=f"Downloaded native working CAD file ({filename}) for {drawing.base_drawing_number} Rev {revision.revision_number}",
        request=request
    )

    with open(revision.native_file.path, "rb") as f:
        response = HttpResponse(f.read(), content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@admin_or_superadmin_required
def grant_access(request, drawing_pk=None):
    """
    Admin / Super Admin assigns access to an individual employee.
    If drawing_pk is provided, applies to that drawing; otherwise global.
    """
    drawing = get_object_or_404(EngineeringDrawing, pk=drawing_pk) if drawing_pk else None

    if request.method == "POST":
        form = DrawingAccessGrantForm(request.POST)
        if form.is_valid():
            access = form.save(commit=False)
            access.drawing = drawing
            access.granted_by = request.user
            access.save()

            target_str = access.user.full_name
            scope_str = drawing.base_drawing_number if drawing else "the entire Drawing Library"

            log_drawing_activity(
                user=request.user,
                action="GRANT_ACCESS",
                drawing=drawing,
                description=f"Granted {access.get_access_level_display()} clearance to employee {target_str} for {scope_str}",
                request=request
            )

            messages.success(request, f"Granted {access.get_access_level_display()} clearance for {scope_str} to {target_str}.")
        else:
            messages.error(request, "Failed to grant clearance. Please ensure an employee is selected.")

    if drawing:
        return redirect("drawings:drawing_detail", pk=drawing.pk)
    return redirect("drawings:global_access_management")


@admin_or_superadmin_required
def revoke_access(request, access_pk):
    """
    Revoke an assigned employee access permission.
    """
    access = get_object_or_404(DrawingAccess, pk=access_pk)
    drawing = access.drawing
    drawing_pk = access.drawing_id
    user_name = access.user.full_name if access.user else "Employee"

    log_drawing_activity(
        user=request.user,
        action="REVOKE_ACCESS",
        drawing=drawing,
        description=f"Revoked drawing library clearance for employee {user_name}",
        request=request
    )

    access.delete()
    messages.success(request, f"Access clearance for {user_name} has been revoked.")
    if drawing_pk:
        return redirect("drawings:drawing_detail", pk=drawing_pk)
    return redirect("drawings:global_access_management")


@admin_or_superadmin_required
def global_access_management(request):
    """
    Super Admin / Admin view:
    1. Comprehensive Activity Logs of every employee in the drawing library.
    2. Simplified Employee Clearance Management (no departments).
    """
    # Activity log filtering
    activity_qs = DrawingActivityLog.objects.select_related("user", "drawing", "revision").all()

    emp_id = request.GET.get("employee", "").strip()
    action_type = request.GET.get("action", "").strip()
    dwg_q = request.GET.get("q", "").strip()

    if emp_id:
        activity_qs = activity_qs.filter(user_id=emp_id)
    if action_type:
        activity_qs = activity_qs.filter(action=action_type)
    if dwg_q:
        activity_qs = activity_qs.filter(
            Q(drawing_number__icontains=dwg_q) |
            Q(description__icontains=dwg_q) |
            Q(drawing__drawing_name__icontains=dwg_q) |
            Q(drawing__customer_name__icontains=dwg_q)
        )

    # Metrics
    total_logs = DrawingActivityLog.objects.count()
    pdf_downloads_count = DrawingActivityLog.objects.filter(action="DOWNLOAD_PDF").count()
    cad_downloads_count = DrawingActivityLog.objects.filter(action="DOWNLOAD_NATIVE").count()
    views_count = DrawingActivityLog.objects.filter(action__in=["VIEW_DRAWING", "VIEW_PDF"]).count()

    # Pagination for activity logs
    paginator = Paginator(activity_qs, 30)
    page_number = request.GET.get("page", 1)
    try:
        logs_page = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        logs_page = paginator.page(1)

    # Employee list for dropdown filter
    all_employees = EmployeeUser.objects.filter(is_active=True).order_by("full_name")

    # Access grants list (purely employee-based, no departments)
    form = DrawingAccessGrantForm()
    global_grants = DrawingAccess.objects.filter(drawing__isnull=True).select_related("user", "granted_by")
    drawing_grants = DrawingAccess.objects.filter(drawing__isnull=False).select_related("drawing", "user", "granted_by")

    return render(request, "drawing_library/global_access.html", {
        "form": form,
        "logs_page": logs_page,
        "total_logs": total_logs,
        "pdf_downloads_count": pdf_downloads_count,
        "cad_downloads_count": cad_downloads_count,
        "views_count": views_count,
        "all_employees": all_employees,
        "action_choices": DrawingActivityLog.ACTION_CHOICES,
        "selected_emp": emp_id,
        "selected_action": action_type,
        "search_q": dwg_q,
        "global_grants": global_grants,
        "drawing_grants": drawing_grants,
    })
