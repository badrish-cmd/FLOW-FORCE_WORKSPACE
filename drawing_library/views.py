import os
import io
from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, Http404, JsonResponse, FileResponse
from django.views.decorators.clickjacking import xframe_options_sameorigin
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
    has_library_edit_access,
    can_create_drawing,
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

    drawings_qs = accessible_drawings
    if q:
        drawings_qs = drawings_qs.filter(
            Q(customer_name__icontains=q) |
            Q(project_name__icontains=q) |
            Q(pid_reference__icontains=q) |
            Q(base_drawing_number__icontains=q) |
            Q(drawing_name__icontains=q) |
            Q(drafter_name__icontains=q)
        )

    drawings = drawings_qs.prefetch_related(
        "revisions",
        "children__revisions",
        "children__children__revisions"
    ).select_related("created_by", "parent")

    # Group drawings under PID project starter references
    pid_groups = {}
    for d in drawings:
        pid = d.pid_reference.strip() if d.pid_reference else "UNASSIGNED"
        if pid not in pid_groups:
            pid_groups[pid] = {
                "pid_reference": pid,
                "customer_name": d.customer_name,
                "project_name": d.project_name or d.drawing_name,
                "drafter_name": d.drafter_name,
                "root_drawing": d.root_drawing,
                "master_count": 0,
                "child_count": 0,
                "grandchild_count": 0,
                "total_drawings": 0,
                "total_revisions": 0,
                "latest_update": d.updated_at,
            }
        group = pid_groups[pid]
        group["total_drawings"] += 1
        group["total_revisions"] += d.revisions.count()

        if d.level == 1:
            group["master_count"] += 1
            if not group.get("drafter_name") and d.drafter_name:
                group["drafter_name"] = d.drafter_name
            if not group.get("project_name") and d.project_name:
                group["project_name"] = d.project_name
            group["root_drawing"] = d
        elif d.level == 2:
            group["child_count"] += 1
        elif d.level == 3:
            group["grandchild_count"] += 1

        if d.updated_at and (not group["latest_update"] or d.updated_at > group["latest_update"]):
            group["latest_update"] = d.updated_at

    # Sort pid_groups by latest_update descending
    sorted_pids = sorted(pid_groups.values(), key=lambda x: x["latest_update"] or timezone.now(), reverse=True)

    # Metrics
    total_projects = len(pid_groups)
    total_drawings = accessible_drawings.count()
    total_revisions = DrawingRevision.objects.filter(drawing__in=accessible_drawings).count()

    is_admin = is_admin_or_superadmin(user)
    can_create = is_admin or has_library_edit_access(user)

    context = {
        "pid_groups": sorted_pids,
        "total_projects": total_projects,
        "total_drawings": total_drawings,
        "total_revisions": total_revisions,
        "search_query": q,
        "is_admin": is_admin,
        "can_create": can_create,
    }
    return render(request, "drawing_library/drawing_list.html", context)


@drawing_library_access_required
def drawing_detail(request, pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=pk)
    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to view this engineering drawing.")
        return redirect("drawings:drawing_list")

    root_project = drawing.root_drawing

    # Log employee view activity
    log_drawing_activity(
        user=request.user,
        action="VIEW_DRAWING",
        drawing=drawing,
        description=f"Viewed project metadata and tree structure for {drawing.base_drawing_number}",
        request=request
    )

    can_edit = has_drawing_access(request.user, drawing, required_level="EDIT") or is_admin_or_superadmin(request.user)
    is_admin = is_admin_or_superadmin(request.user)

    revisions = drawing.revisions.all().order_by("-revision_date", "-created_at")
    revision_form = DrawingRevisionForm(initial={"drafter_name": drawing.drafter_name})
    access_form = DrawingAccessGrantForm() if is_admin else None

    # Load all Level 1 (Parent Branch) drawings associated with this root project / PID
    # Prefetch children (Level 2: sub-assemblies) and their children (Level 3: detail parts)
    tree_drawings = EngineeringDrawing.objects.filter(
        Q(pk=root_project.pk) | Q(pid_reference=root_project.pid_reference, parent__isnull=True)
    ).distinct().prefetch_related(
        "revisions",
        "children__revisions",
        "children__children__revisions",
    ).order_by("base_drawing_number")

    # Other drawings saved under this same PID reference
    other_pid_drawings = EngineeringDrawing.objects.filter(
        pid_reference=drawing.pid_reference
    ).exclude(pk=drawing.pk).prefetch_related("revisions")

    # Access grants for this drawing
    access_grants = drawing.access_grants.select_related("user", "granted_by") if is_admin else []
    
    # Recent activity logs for this drawing (visible to admin)
    recent_activities = drawing.activity_logs.select_related("user").order_by("-created_at")[:20] if is_admin else []

    context = {
        "drawing": drawing,
        "root_project": root_project,
        "tree_drawings": tree_drawings,
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
    parent_id = request.GET.get("parent_id") or request.POST.get("parent")
    parent_drawing = None
    if parent_id:
        try:
            parent_drawing = EngineeringDrawing.objects.get(pk=parent_id)
        except (EngineeringDrawing.DoesNotExist, ValueError):
            parent_drawing = None

    if not can_create_drawing(request.user, parent_drawing):
        messages.error(request, "Access Denied: You do not have permission to create drawings here.")
        return redirect("drawings:drawing_list")

    # Existing PIDs list for easy autocomplete / datalist
    existing_pids = list(
        EngineeringDrawing.objects.values_list("pid_reference", flat=True)
        .distinct()
        .order_by("pid_reference")
    )

    if request.method == "POST":
        post_data = request.POST.copy()
        # Auto-inherit project metadata from parent or root project context
        ref_drawing = parent_drawing
        return_pk = post_data.get("return_to_pk")
        if not ref_drawing and return_pk:
            try:
                ref_drawing = EngineeringDrawing.objects.get(pk=return_pk)
            except (EngineeringDrawing.DoesNotExist, ValueError):
                ref_drawing = None

        if ref_drawing:
            if not post_data.get("customer_name") and ref_drawing.customer_name:
                post_data["customer_name"] = ref_drawing.customer_name
            if not post_data.get("project_name") and ref_drawing.project_name:
                post_data["project_name"] = ref_drawing.project_name
            if not post_data.get("pid_reference") and ref_drawing.pid_reference:
                post_data["pid_reference"] = ref_drawing.pid_reference
            if not post_data.get("drafter_name") and ref_drawing.drafter_name:
                post_data["drafter_name"] = ref_drawing.drafter_name

        form = EngineeringDrawingForm(post_data, request.FILES)
        if form.is_valid():
            drawing = form.save(commit=False)
            drawing.created_by = request.user
            if parent_drawing:
                drawing.parent = parent_drawing
                if parent_drawing.level == 1:
                    drawing.drawing_type = "SUB_ASSEMBLY"
                else:
                    drawing.drawing_type = "DETAIL_PART"
            if not drawing.project_name and ref_drawing:
                drawing.project_name = ref_drawing.project_name
            if not drawing.customer_name and ref_drawing:
                drawing.customer_name = ref_drawing.customer_name
            if not drawing.pid_reference and ref_drawing:
                drawing.pid_reference = ref_drawing.pid_reference
            if not drawing.watermark_company and ref_drawing:
                drawing.watermark_company = ref_drawing.watermark_company
            drawing.save()

            log_drawing_activity(
                user=request.user,
                action="CREATE_DRAWING",
                drawing=drawing,
                description=f"Created {drawing.get_drawing_type_display()} '{drawing.drawing_name}' ({drawing.base_drawing_number}) under PID {drawing.pid_reference}",
                request=request
            )

            # Handle optional initial revision if files or notes are provided
            init_rev = form.cleaned_data.get("initial_revision_number") or "0"
            init_desc = form.cleaned_data.get("initial_stage_description") or "Initial Release"
            init_native = form.cleaned_data.get("initial_native_file")
            init_pdf = form.cleaned_data.get("initial_pdf_file")

            if init_native or init_pdf or init_rev:
                rev = DrawingRevision.objects.create(
                    drawing=drawing,
                    revision_number=init_rev,
                    stage_change_description=init_desc,
                    native_file=init_native,
                    pdf_file=init_pdf,
                    original_pdf_filename=os.path.basename(init_pdf.name) if init_pdf else "",
                    original_native_filename=os.path.basename(init_native.name) if init_native else "",
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

            messages.success(request, f"Drawing '{drawing.drawing_name}' ({drawing.base_drawing_number}) saved under PID '{drawing.pid_reference}'.")
            return redirect("drawings:drawing_detail", pk=drawing.root_drawing.pk)
        else:
            # Extract errors clearly
            err_msgs = []
            for field, errs in form.errors.items():
                err_msgs.append(f"{field}: {', '.join(errs)}")
            err_summary = "; ".join(err_msgs)
            messages.error(request, f"Could not create drawing: {err_summary}")

            # If submitted from drawing_detail modal, redirect back smoothly
            return_pk = request.POST.get("return_to_pk")
            if not return_pk and parent_drawing:
                return_pk = parent_drawing.root_drawing.pk
            if return_pk:
                return redirect("drawings:drawing_detail", pk=return_pk)
    else:
        initial_data = {}
        if parent_drawing:
            initial_data["parent"] = parent_drawing.id
            initial_data["customer_name"] = parent_drawing.customer_name
            initial_data["pid_reference"] = parent_drawing.pid_reference
            initial_data["project_name"] = parent_drawing.project_name
            initial_data["drafter_name"] = parent_drawing.drafter_name
            if parent_drawing.level == 1:
                initial_data["drawing_type"] = "SUB_ASSEMBLY"
            elif parent_drawing.level == 2:
                initial_data["drawing_type"] = "DETAIL_PART"
        if request.GET.get("pid"):
            initial_data["pid_reference"] = request.GET.get("pid")
        if request.GET.get("customer"):
            initial_data["customer_name"] = request.GET.get("customer")
        if request.GET.get("project_name"):
            initial_data["project_name"] = request.GET.get("project_name")
        if request.GET.get("drafter_name"):
            initial_data["drafter_name"] = request.GET.get("drafter_name")
        form = EngineeringDrawingForm(initial=initial_data)

    form_title = "New Engineering Drawing Project"
    if parent_drawing:
        if parent_drawing.level == 1:
            form_title = f"Add Sub-Assembly (Level 2 Child) for {parent_drawing.base_drawing_number}"
        elif parent_drawing.level == 2:
            form_title = f"Add Detail Part (Level 3 Grandchild) for {parent_drawing.base_drawing_number}"

    return render(request, "drawing_library/drawing_form.html", {
        "form": form,
        "existing_pids": existing_pids,
        "parent_drawing": parent_drawing,
        "title": form_title,
        "is_create": True,
    })


@drawing_library_access_required
def drawing_edit(request, pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=pk)
    if not (is_admin_or_superadmin(request.user) or has_drawing_access(request.user, drawing, required_level="EDIT")):
        messages.error(request, "Access Denied: You do not have permission to edit this drawing.")
        return redirect("drawings:drawing_detail", pk=pk)

    existing_pids = list(
        EngineeringDrawing.objects.values_list("pid_reference", flat=True)
        .distinct()
        .order_by("pid_reference")
    )

    if request.method == "POST":
        form = EngineeringDrawingForm(request.POST, request.FILES, instance=drawing)
        if form.is_valid():
            form.save()

            # Handle optional revision files attached during drawing edit
            init_rev = form.cleaned_data.get("initial_revision_number") or drawing.active_revision_number
            init_desc = form.cleaned_data.get("initial_stage_description") or "Updated via Edit Form"
            init_native = form.cleaned_data.get("initial_native_file")
            init_pdf = form.cleaned_data.get("initial_pdf_file")

            if init_native or init_pdf:
                latest = drawing.latest_revision
                if latest and not latest.native_file and not latest.pdf_file:
                    if init_native:
                        latest.native_file = init_native
                        latest.original_native_filename = os.path.basename(init_native.name)
                    if init_pdf:
                        latest.pdf_file = init_pdf
                        latest.original_pdf_filename = os.path.basename(init_pdf.name)
                    if init_rev:
                        latest.revision_number = init_rev
                    if init_desc:
                        latest.stage_change_description = init_desc
                    latest.save()
                    drawing.sync_active_revision()
                else:
                    rev_num = init_rev if (init_rev and (not latest or init_rev != latest.revision_number)) else (
                        str(int(drawing.active_revision_number) + 1) if drawing.active_revision_number.isdigit() else f"{drawing.active_revision_number}.1"
                    )
                    rev = DrawingRevision.objects.create(
                        drawing=drawing,
                        revision_number=rev_num,
                        stage_change_description=init_desc,
                        native_file=init_native,
                        pdf_file=init_pdf,
                        original_pdf_filename=os.path.basename(init_pdf.name) if init_pdf else "",
                        original_native_filename=os.path.basename(init_native.name) if init_native else "",
                        drafter_name=drawing.drafter_name,
                        created_by=request.user,
                    )
                    drawing.sync_active_revision()

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
        latest_rev = drawing.latest_revision
        initial_data = {
            "initial_revision_number": drawing.active_revision_number,
            "initial_stage_description": latest_rev.stage_change_description if latest_rev else "Initial Release",
        }
        form = EngineeringDrawingForm(instance=drawing, initial=initial_data)

    return render(request, "drawing_library/drawing_form.html", {
        "form": form,
        "drawing": drawing,
        "existing_pids": existing_pids,
        "title": f"Edit Drawing - {drawing.base_drawing_number}",
        "is_create": False,
    })


@drawing_library_access_required
def drawing_delete(request, pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=pk)
    root_drawing = drawing.root_drawing
    root_pk = root_drawing.pk
    is_root = (drawing.pk == root_pk)
    pid_ref = drawing.pid_reference

    if not (is_admin_or_superadmin(request.user) or has_drawing_access(request.user, drawing, required_level="EDIT")):
        messages.error(request, "Access Denied: You do not have permission to delete this drawing.")
        return redirect("drawings:drawing_detail", pk=root_pk)

    if request.method == "POST":
        drawing_num = drawing.base_drawing_number
        if drawing.level == 1:
            level_name = "Parent Drawing"
        elif drawing.level == 2:
            level_name = "Sub-Assembly (Child)"
        else:
            level_name = "Detail Part (Grandchild)"

        log_drawing_activity(
            user=request.user,
            action="DELETE_DRAWING",
            drawing=drawing,
            description=f"Deleted {level_name} {drawing_num} ({drawing.drawing_name}) and all associated revisions",
            request=request
        )
        drawing.delete()
        messages.success(request, f"{level_name} '{drawing_num}' and its revisions have been permanently deleted.")

        next_url = request.POST.get("next")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)

        if not is_root:
            return redirect("drawings:drawing_detail", pk=root_pk)

        # Root drawing deleted: check if another parent drawing exists for this PID
        other_root = EngineeringDrawing.objects.filter(
            pid_reference=pid_ref, parent__isnull=True
        ).exclude(pk=root_pk).first()
        if other_root:
            return redirect("drawings:drawing_detail", pk=other_root.pk)
        return redirect("drawings:drawing_list")

    return redirect("drawings:drawing_detail", pk=root_pk)



@drawing_library_access_required
def revision_create(request, drawing_pk):
    drawing = get_object_or_404(EngineeringDrawing, pk=drawing_pk)
    root_pk = drawing.root_drawing.pk

    if not (is_admin_or_superadmin(request.user) or has_drawing_access(request.user, drawing, required_level="EDIT")):
        messages.error(request, "You do not have permission to upload revisions to this drawing.")
        return redirect("drawings:drawing_detail", pk=root_pk)

    if request.method == "POST":
        form = DrawingRevisionForm(request.POST, request.FILES)
        if form.is_valid():
            revision = form.save(commit=False)
            revision.drawing = drawing
            revision.created_by = request.user
            if "pdf_file" in request.FILES:
                revision.original_pdf_filename = os.path.basename(request.FILES["pdf_file"].name)
            if "native_file" in request.FILES:
                revision.original_native_filename = os.path.basename(request.FILES["native_file"].name)
            revision.save()
            drawing.sync_active_revision()

            log_drawing_activity(
                user=request.user,
                action="UPLOAD_REVISION",
                drawing=drawing,
                revision=revision,
                description=f"Uploaded Rev {revision.revision_number} for {drawing.base_drawing_number}: {revision.stage_change_description[:80]}",
                request=request
            )

            messages.success(request, f"Revision {revision.revision_number} uploaded successfully for {drawing.base_drawing_number} ({drawing.drawing_name}).")
            return redirect("drawings:drawing_detail", pk=root_pk)
        else:
            err_details = []
            for field, err_list in form.errors.items():
                err_details.append(f"{field}: {', '.join(err_list)}")
            err_str = "; ".join(err_details) if err_details else "Invalid submission"
            messages.error(request, f"Revision upload failed: {err_str}")
            return redirect("drawings:drawing_detail", pk=root_pk)

    return redirect("drawings:drawing_detail", pk=root_pk)


@drawing_library_access_required
def revision_delete(request, revision_pk):
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing
    root_pk = drawing.root_drawing.pk
    rev_num = revision.revision_number

    if not (is_admin_or_superadmin(request.user) or has_drawing_access(request.user, drawing, required_level="EDIT")):
        messages.error(request, "Access Denied: You do not have permission to delete revisions for this drawing.")
        return redirect("drawings:drawing_detail", pk=root_pk)

    if request.method == "POST":
        log_drawing_activity(
            user=request.user,
            action="DELETE_REVISION",
            drawing=drawing,
            description=f"Deleted Revision {rev_num} from drawing {drawing.base_drawing_number}",
            request=request
        )

        revision.delete()
        drawing.sync_active_revision()
        messages.success(request, f"Revision {rev_num} has been removed from {drawing.base_drawing_number}.")
    return redirect("drawings:drawing_detail", pk=root_pk)



@drawing_library_access_required
def download_watermarked_pdf(request, revision_pk):
    """
    Download the exported PDF clean without any watermark,
    strictly retaining the original uploaded filename.
    """
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing

    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to download this PDF.")
        return redirect("drawings:drawing_list")

    if not revision.pdf_file or not os.path.exists(revision.pdf_file.path):
        raise Http404("PDF file attachment not found.")

    filename = revision.expected_pdf_filename()

    # Log employee download activity
    log_drawing_activity(
        user=request.user,
        action="DOWNLOAD_PDF",
        drawing=drawing,
        revision=revision,
        description=f"Downloaded clean PDF ({filename}) for {drawing.base_drawing_number} Rev {revision.revision_number}",
        request=request
    )

    with open(revision.pdf_file.path, "rb") as f:
        pdf_data = f.read()

    response = HttpResponse(pdf_data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@xframe_options_sameorigin
@drawing_library_access_required
def view_watermarked_pdf(request, revision_pk):
    """
    View the clean PDF directly in the browser (inline viewer/iframe)
    without any watermark.
    """
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing

    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to view this PDF.")
        return redirect("drawings:drawing_list")

    if not revision.pdf_file or not os.path.exists(revision.pdf_file.path):
        raise Http404("PDF file attachment not found.")

    filename = revision.expected_pdf_filename()

    # Log employee preview activity
    log_drawing_activity(
        user=request.user,
        action="VIEW_PDF",
        drawing=drawing,
        revision=revision,
        description=f"Previewed clean PDF for {drawing.base_drawing_number} Rev {revision.revision_number} in browser",
        request=request
    )

    response = FileResponse(open(revision.pdf_file.path, "rb"), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


@drawing_library_access_required
def download_native_file(request, revision_pk):
    """
    Download the native CAD working file (.dwt, .slddrw, .dwg, etc.),
    strictly retaining the original uploaded filename.
    """
    revision = get_object_or_404(DrawingRevision, pk=revision_pk)
    drawing = revision.drawing

    if not has_drawing_access(request.user, drawing, required_level="VIEW"):
        messages.error(request, "Access Denied: You do not have permission to download this native file.")
        return redirect("drawings:drawing_list")

    if not revision.native_file or not os.path.exists(revision.native_file.path):
        raise Http404("Native file attachment not found.")

    filename = revision.expected_native_filename()

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
