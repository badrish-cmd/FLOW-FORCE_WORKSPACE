"""
Views for Tickets & Service Jobs module with Synology NAS file management.
"""
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import uuid
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import (FileResponse, Http404, HttpResponse,
                         HttpResponseForbidden, JsonResponse,
                         StreamingHttpResponse)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import TicketCreateForm, TicketUpdateForm
from .models import Ticket, TicketEvent, TicketFile, TicketShareLink
from .services import file_handler, synology, workflow, zoho_lookup
from .tasks import (download_file_from_link_task, process_zip_file_task,
                    push_file_to_synology)

logger = logging.getLogger(__name__)
User = get_user_model()


@login_required
def ticket_list(request):
    """
    List tickets with tabs for Projects, Service, and All.
    Supports filtering by status, priority, assignee, search, and expiring warranties.
    """
    ticket_type = request.GET.get("type", "").upper()
    status_filter = request.GET.get("status", "")
    priority_filter = request.GET.get("priority", "")
    assigned_filter = request.GET.get("assigned_to", "")
    filter_preset = request.GET.get("filter", "")
    search_q = request.GET.get("q", "").strip()

    qs = Ticket.objects.filter(is_deleted=False).select_related("assigned_to", "created_by")

    if ticket_type in (Ticket.TYPE_PROJECT, Ticket.TYPE_SERVICE):
        qs = qs.filter(type=ticket_type)

    if status_filter:
        qs = qs.filter(status=status_filter)

    if priority_filter:
        qs = qs.filter(priority=priority_filter)

    if assigned_filter:
        if assigned_filter == "me":
            qs = qs.filter(assigned_to=request.user)
        elif assigned_filter.isdigit():
            qs = qs.filter(assigned_to_id=int(assigned_filter))

    today = timezone.localdate()
    closed_states = [Ticket.STATUS_CLOSED]
    if filter_preset == "expiring_warranty":
        in_30_days = today + timezone.timedelta(days=30)
        qs = qs.filter(warranty_end__gte=today, warranty_end__lte=in_30_days)
    elif filter_preset == "my_tickets":
        qs = qs.filter(assigned_to=request.user)
    elif filter_preset == "urgent":
        qs = qs.filter(priority__in=[Ticket.PRIORITY_URGENT, Ticket.PRIORITY_HIGH]).exclude(status__in=closed_states)
    elif filter_preset == "open":
        qs = qs.exclude(status__in=closed_states)

    if search_q:
        qs = qs.filter(
            Q(number__icontains=search_q)
            | Q(title__icontains=search_q)
            | Q(customer_name__icontains=search_q)
            | Q(job_no__icontains=search_q)
            | Q(enquiry_number__icontains=search_q)
            | Q(client_contact_name__icontains=search_q)
            | Q(quote_number__icontains=search_q)
        )

    qs = qs.order_by("-updated_at")

    # One grouped query for every count on the page
    base = Ticket.objects.filter(is_deleted=False)
    counts = {(r["type"], r["status"]): r["n"] for r in base.values("type", "status").annotate(n=Count("id"))}

    def pipeline(ticket_type, choices):
        steps = [{"key": k, "label": lbl, "count": counts.get((ticket_type, k), 0)} for k, lbl in choices]
        return {"steps": steps, "open": sum(x["count"] for x in steps if x["key"] not in closed_states),
                "total": sum(x["count"] for x in steps)}

    project_pipe = pipeline(Ticket.TYPE_PROJECT, Ticket.PROJECT_STATUS_CHOICES)
    service_pipe = pipeline(Ticket.TYPE_SERVICE, Ticket.SERVICE_STATUS_CHOICES)
    in_30_days = today + timezone.timedelta(days=30)
    expiring_count = base.filter(warranty_end__gte=today, warranty_end__lte=in_30_days).count()
    urgent_count = base.filter(priority__in=[Ticket.PRIORITY_URGENT, Ticket.PRIORITY_HIGH]).exclude(status__in=closed_states).count()
    mine_count = base.filter(assigned_to=request.user).exclude(status__in=closed_states).count()

    if ticket_type == Ticket.TYPE_PROJECT:
        status_options = Ticket.PROJECT_STATUS_CHOICES
    elif ticket_type == Ticket.TYPE_SERVICE:
        status_options = Ticket.SERVICE_STATUS_CHOICES
    else:
        seen, status_options = set(), []
        for k, lbl in Ticket.PROJECT_STATUS_CHOICES + Ticket.SERVICE_STATUS_CHOICES:
            if k not in seen:
                seen.add(k)
                status_options.append((k, lbl))

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    keep = request.GET.copy()
    keep.pop("page", None)

    users = User.objects.filter(is_active=True).order_by("full_name", "email")

    context = {
        "page_obj": page_obj,
        "selected_type": ticket_type,
        "status_filter": status_filter,
        "priority_filter": priority_filter,
        "assigned_filter": assigned_filter,
        "filter_preset": filter_preset,
        "search_q": search_q,
        "project_pipe": project_pipe,
        "service_pipe": service_pipe,
        "expiring_count": expiring_count,
        "urgent_count": urgent_count,
        "mine_count": mine_count,
        "status_options": status_options,
        "priority_options": Ticket.PRIORITY_CHOICES,
        "querystring": keep.urlencode(),
        "has_filters": bool(search_q or status_filter or priority_filter or assigned_filter or filter_preset or ticket_type),
        "users": users,
        "can_create": workflow.can_create_ticket(request.user),
    }
    return render(request, "tickets/ticket_list.html", context)


@login_required
def ticket_create(request):
    """
    Create a new Project Job or Service Ticket.
    Includes live Zoho auto-fill by JOB No or Enquiry No.
    """
    if not workflow.can_create_ticket(request.user):
        raise PermissionDenied("You do not have permission to create tickets.")

    initial_type = request.GET.get("type", Ticket.TYPE_PROJECT).upper()
    if initial_type not in (Ticket.TYPE_PROJECT, Ticket.TYPE_SERVICE):
        initial_type = Ticket.TYPE_PROJECT

    parent_job_id = request.GET.get("parent_job")
    parent_job = None
    if parent_job_id:
        parent_job = Ticket.objects.filter(id=parent_job_id, type=Ticket.TYPE_PROJECT, is_deleted=False).first()

    if request.method == "POST":
        form = TicketCreateForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                ticket = form.save(commit=False)
                ticket.created_by = request.user
                if parent_job and ticket.type == Ticket.TYPE_SERVICE:
                    ticket.parent_job = parent_job

                # Pre-calculate Synology NAS folder path
                ticket.nas_folder = synology.build_ticket_nas_folder(ticket)
                ticket.save()

                # Record creation event
                TicketEvent.objects.create(
                    ticket=ticket,
                    user=request.user,
                    action="CREATED",
                    to_status=ticket.status,
                    note="Ticket created."
                )

            # Trigger Synology folder structure creation in background / safe call
            try:
                client = synology.get_synology_client()
                client.create_ticket_folder_structure(ticket)
            except Exception as e:
                logger.warning("Could not immediately create Synology folder for %s: %s", ticket.number, e)

            messages.success(request, f"Ticket {ticket.number} successfully created.")
            return redirect("tickets:ticket_detail", ticket_id=ticket.id)
    else:
        initial = {"type": initial_type}
        if parent_job:
            initial["parent_job"] = parent_job
            initial["customer_name"] = parent_job.customer_name
            initial["job_no"] = parent_job.job_no
            initial["site_address"] = parent_job.site_address
        form = TicketCreateForm(initial=initial)

    return render(request, "tickets/ticket_create.html", {
        "form": form,
        "initial_type": initial_type,
        "parent_job": parent_job,
    })


@login_required
def ticket_detail(request, ticket_id):
    """
    Detailed view of a ticket:
    - Status progression stepper
    - Header and customer details
    - Categorized file sections with gallery
    - Synology share links
    - Timeline audit trail
    """
    ticket = get_object_or_404(Ticket.objects.select_related("assigned_to", "created_by", "parent_job"), id=ticket_id)
    if ticket.is_deleted:
        raise Http404("This ticket has been deleted.")

    if not workflow.can_view(request.user, ticket):
        raise PermissionDenied("You do not have permission to view this ticket.")

    # Group active files by category
    files_qs = ticket.files.filter(is_hidden=False).select_related("uploaded_by").order_by("-uploaded_at")
    files_by_category = {
        TicketFile.CATEGORY_CLIENT_DETAILS: [],
        TicketFile.CATEGORY_PHOTO: [],
        TicketFile.CATEGORY_SITE_REPORT: [],
        TicketFile.CATEGORY_WARRANTY: [],
        TicketFile.CATEGORY_RECEIPT: [],
        TicketFile.CATEGORY_OTHER: [],
    }
    for f in files_qs:
        cat = f.category if f.category in files_by_category else TicketFile.CATEGORY_OTHER
        files_by_category[cat].append(f)

    # Linked service tickets if this is a project
    child_services = []
    if ticket.type == Ticket.TYPE_PROJECT:
        child_services = ticket.service_tickets.filter(is_deleted=False).order_by("-created_at")

    # Permissions
    can_edit = workflow.can_edit_ticket(request.user, ticket)
    can_change_status = workflow.can_change_status(request.user, ticket)
    can_delete = workflow.can_delete_ticket(request.user, ticket)
    can_override = workflow.can_override_status_rules(request.user)

    events = ticket.events.select_related("user").order_by("-created_at")
    share_links = ticket.share_links.filter(expires_at__gte=timezone.now()).select_related("created_by")

    max_upload_mb = getattr(settings, "TICKETS_MAX_UPLOAD_MB", 5120)

    context = {
        "ticket": ticket,
        "files_by_category": files_by_category,
        "all_files_count": files_qs.count(),
        "child_services": child_services,
        "events": events,
        "share_links": share_links,
        "can_edit": can_edit,
        "can_change_status": can_change_status,
        "can_delete": can_delete,
        "can_override": can_override,
        "max_upload_mb": max_upload_mb,
        "synology_configured": synology.is_configured(),
        "categories": TicketFile.CATEGORY_CHOICES,
    }
    return render(request, "tickets/ticket_detail.html", context)


@login_required
def ticket_update(request, ticket_id):
    """Edit ticket metadata, customer information, dates, and assignment."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    if not workflow.can_edit_ticket(request.user, ticket):
        raise PermissionDenied("You do not have permission to edit this ticket.")

    if request.method == "POST":
        form = TicketUpdateForm(request.POST, instance=ticket)
        if form.is_valid():
            form.save()
            TicketEvent.objects.create(
                ticket=ticket,
                user=request.user,
                action="UPDATED",
                note="Ticket details updated."
            )
            messages.success(request, "Ticket details updated successfully.")
            return redirect("tickets:ticket_detail", ticket_id=ticket.id)
    else:
        form = TicketUpdateForm(instance=ticket)

    return render(request, "tickets/ticket_update.html", {"form": form, "ticket": ticket})


@login_required
@require_POST
def ticket_status_change(request, ticket_id):
    """Advance or change ticket status with required document validation."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    new_status = request.POST.get("new_status", "").strip()
    note = request.POST.get("note", "").strip()
    override_reason = request.POST.get("override_reason", "").strip()

    valid_statuses = ticket.status_list
    if new_status not in valid_statuses:
        messages.error(request, "Invalid status requested.")
        return redirect("tickets:ticket_detail", ticket_id=ticket.id)

    try:
        workflow.change_ticket_status(
            ticket=ticket,
            new_status=new_status,
            user=request.user,
            note=note,
            override_reason=override_reason
        )
        messages.success(request, f"Status changed to {ticket.status_label}.")
    except ValidationError as e:
        messages.error(request, str(e.message if hasattr(e, "message") else e))
    except PermissionDenied as e:
        messages.error(request, str(e))

    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
@require_POST
def ticket_add_note(request, ticket_id):
    """Add a note or comment to ticket timeline."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    note = request.POST.get("note", "").strip()

    if not note:
        messages.error(request, "Note content cannot be empty.")
        return redirect("tickets:ticket_detail", ticket_id=ticket.id)

    TicketEvent.objects.create(
        ticket=ticket,
        user=request.user,
        action="NOTE_ADDED",
        note=note
    )
    user_name = getattr(request.user, "full_name", None) or getattr(request.user, "email", "") or "User"
    workflow.notify_ticket_event(
        ticket=ticket,
        title=f"New note on Ticket {ticket.number}",
        message=f"{user_name} wrote: {note}",
        kind="NOTE",
        exclude_user=request.user
    )

    messages.success(request, "Note added.")
    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
@require_POST
def ticket_upload_chunk(request, ticket_id):
    """
    Handle chunked file uploads (supports files up to 5 GB via 10 MB chunks).
    When the final chunk arrives, reassembles file, checks duplicates, generates thumbnail,
    and queues background sync to Synology NAS.
    """
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)

    chunk = request.FILES.get("chunk")
    upload_uuid = request.POST.get("upload_uuid", "").strip()
    chunk_index = int(request.POST.get("chunk_index", 0))
    total_chunks = int(request.POST.get("total_chunks", 1))
    original_filename = request.POST.get("filename", "").strip()
    category = request.POST.get("category", TicketFile.CATEGORY_OTHER).strip()

    if not chunk or not upload_uuid or not original_filename:
        return JsonResponse({"error": "Missing required chunk upload parameters."}, status=400)

    # Sanitize upload uuid to prevent directory traversal
    clean_uuid = synology.sanitize_path_segment(upload_uuid, max_length=64)
    chunks_dir = os.path.join(tempfile.gettempdir(), "flowforce_chunks", clean_uuid)
    os.makedirs(chunks_dir, exist_ok=True)

    chunk_path = os.path.join(chunks_dir, f"part_{chunk_index:05d}")
    with open(chunk_path, "wb") as f:
        for c in chunk.chunks():
            f.write(c)

    # Check if all chunks have arrived
    parts = os.listdir(chunks_dir)
    if len(parts) < total_chunks:
        return JsonResponse({"status": "chunk_received", "received": len(parts), "total": total_chunks})

    # Assemble complete file
    assembled_dir = os.path.join(settings.MEDIA_ROOT, "tickets", "uploads", timezone.localdate().strftime("%Y/%m"))
    os.makedirs(assembled_dir, exist_ok=True)

    safe_name = synology.sanitize_path_segment(original_filename)
    assembled_path = os.path.join(assembled_dir, f"{clean_uuid}_{safe_name}")

    with open(assembled_path, "wb") as dst:
        for idx in range(total_chunks):
            part_file = os.path.join(chunks_dir, f"part_{idx:05d}")
            if not os.path.exists(part_file):
                return JsonResponse({"error": f"Missing part {idx} during assembly."}, status=400)
            with open(part_file, "rb") as src:
                shutil.copyfileobj(src, dst)

    # Remove temporary chunk directory
    try:
        shutil.rmtree(chunks_dir)
    except Exception:
        pass

    file_size = os.path.getsize(assembled_path)
    sha = file_handler.compute_sha256(assembled_path)

    # Duplicate check
    if TicketFile.objects.filter(ticket=ticket, sha256=sha, is_hidden=False).exists():
        try:
            os.remove(assembled_path)
        except Exception:
            pass
        return JsonResponse({"status": "duplicate", "message": "This file has already been uploaded to this ticket."})

    # HEIC conversion if applicable
    ext = os.path.splitext(original_filename)[1].lower()
    if ext in (".heic", ".heif") and file_handler.HEIF_SUPPORTED:
        converted_jpg = file_handler.convert_heic_to_jpeg(assembled_path)
        if converted_jpg:
            logger.info("Created converted JPEG for %s", original_filename)

    mime = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
    stored_name = file_handler.generate_stored_filename(ticket, category, original_filename)

    # Thumbnail generation
    thumb = None
    if mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        thumb = file_handler.generate_thumbnail(assembled_path)

    # Check if uploaded file is a ZIP archive
    is_zip = ext == ".zip" or mime in ("application/zip", "application/x-zip-compressed")

    tf = TicketFile.objects.create(
        ticket=ticket,
        category=category,
        original_name=original_filename,
        stored_name=stored_name,
        size_bytes=file_size,
        mime_type=mime,
        sha256=sha,
        thumbnail=thumb,
        source=TicketFile.SOURCE_UPLOAD,
        sync_status=TicketFile.SYNC_PENDING,
        local_temp_path=assembled_path,
        uploaded_by=request.user,
    )

    TicketEvent.objects.create(
        ticket=ticket,
        user=request.user,
        action="FILE_UPLOADED",
        note=f"Uploaded file {original_filename} ({tf.readable_size}) to {tf.get_category_display()}."
    )

    # Trigger async background tasks
    if is_zip:
        process_zip_file_task.delay(tf.id, request.user.id)
    else:
        push_file_to_synology.delay(tf.id)

    return JsonResponse({
        "status": "success",
        "file_id": tf.id,
        "filename": tf.original_name,
        "stored_name": tf.stored_name,
        "size": tf.readable_size,
    })


@login_required
@require_POST
def ticket_add_from_link(request, ticket_id):
    """Download a file from an external link with SSRF protection in background."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    url = request.POST.get("url", "").strip()
    category = request.POST.get("category", TicketFile.CATEGORY_OTHER).strip()

    if not url:
        messages.error(request, "Please enter a valid link.")
        return redirect("tickets:ticket_detail", ticket_id=ticket.id)

    try:
        file_handler.validate_url_security(url)
    except ValidationError as e:
        messages.error(request, str(e.message if hasattr(e, "message") else e))
        return redirect("tickets:ticket_detail", ticket_id=ticket.id)

    # Queue background task
    download_file_from_link_task.delay(ticket.id, url, category, request.user.id)
    messages.info(request, "Download from link initiated. Files will appear once downloaded and processed.")
    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
@require_POST
def ticket_create_share_link(request, ticket_id):
    """Generate a Synology File Station share link with expiration."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    file_id = request.POST.get("file_id")
    days = int(request.POST.get("days", 7))

    tf = None
    target_path = ticket.nas_folder
    if file_id:
        tf = get_object_or_404(TicketFile, id=file_id, ticket=ticket)
        target_path = tf.nas_path or posixpath.join(ticket.nas_folder, TicketFile.CATEGORY_FOLDER_MAP.get(tf.category, ""), tf.stored_name)

    if not synology.is_configured():
        messages.warning(request, "Synology integration is not configured. Share links are unavailable.")
        return redirect("tickets:ticket_detail", ticket_id=ticket.id)

    try:
        client = synology.get_synology_client()
        result = client.create_share_link(target_path, days=days)
        if result and result.get("url"):
            expires_at = timezone.now() + timezone.timedelta(days=days)
            share_link = TicketShareLink.objects.create(
                ticket=ticket,
                ticket_file=tf,
                folder_path=target_path if not tf else "",
                url=result["url"],
                expires_at=expires_at,
                created_by=request.user,
            )
            messages.success(request, f"Share link generated successfully (expires in {days} days): {result['url']}")
        else:
            messages.error(request, "Synology did not return a share link URL.")
    except Exception as e:
        logger.error("Failed to create share link: %s", e)
        messages.error(request, f"Could not create share link: {e}")

    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
def ticket_file_download(request, ticket_id, file_id):
    """Download a file locally or stream from Synology NAS."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    tf = get_object_or_404(TicketFile, id=file_id, ticket=ticket, is_hidden=False)

    if not workflow.can_view(request.user, ticket):
        raise PermissionDenied("You do not have permission to download files for this ticket.")

    # 1. Local temp file available
    if tf.local_temp_path and os.path.exists(tf.local_temp_path):
        resp = FileResponse(open(tf.local_temp_path, "rb"), as_attachment=True, filename=tf.original_name)
        if tf.mime_type:
            resp["Content-Type"] = tf.mime_type
        return resp

    # 2. Synology NAS stream
    if tf.nas_path and synology.is_configured():
        try:
            client = synology.get_synology_client()
            stream = client.download_file_stream(tf.nas_path)
            response = StreamingHttpResponse(stream.iter_content(chunk_size=65536), content_type=tf.mime_type or "application/octet-stream")
            response["Content-Disposition"] = f'attachment; filename="{tf.original_name}"'
            return response
        except Exception as e:
            logger.warning("Could not stream file %s from Synology: %s", tf.id, e)
            messages.error(request, f"Could not retrieve file from Synology NAS: {e}")
            return redirect("tickets:ticket_detail", ticket_id=ticket.id)

    messages.error(request, "File is not currently available for download.")
    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
@require_POST
def ticket_file_delete(request, ticket_id, file_id):
    """Soft delete a file from ticket (sets is_hidden=True). Never deletes on Synology."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    tf = get_object_or_404(TicketFile, id=file_id, ticket=ticket)

    if not workflow.can_edit_ticket(request.user, ticket):
        raise PermissionDenied("You do not have permission to remove files from this ticket.")

    tf.is_hidden = True
    tf.save(update_fields=["is_hidden"])

    TicketEvent.objects.create(
        ticket=ticket,
        user=request.user,
        action="FILE_REMOVED",
        note=f"Removed file {tf.original_name} from ticket."
    )
    messages.success(request, f"File {tf.original_name} removed.")
    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
@require_POST
def ticket_delete(request, ticket_id):
    """Soft delete a ticket (SUPER_ADMIN or Ticket Managers only)."""
    ticket = get_object_or_404(Ticket, id=ticket_id, is_deleted=False)
    reason = request.POST.get("reason", "").strip()

    try:
        workflow.soft_delete_ticket(ticket, request.user, reason=reason)
        messages.success(request, f"Ticket {ticket.number} has been deleted.")
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("tickets:ticket_detail", ticket_id=ticket.id)

    return redirect("tickets:ticket_list")


@login_required
@require_GET
def zoho_lookup_api(request):
    """
    AJAX endpoint for live lookup of customer and job details from Zoho Books.
    Query parameters: ?job_no=... or ?enquiry_number=...
    """
    job_no = request.GET.get("job_no", "").strip()
    enquiry_number = request.GET.get("enquiry_number", "").strip()

    if job_no:
        data = zoho_lookup.lookup_by_job_no(job_no)
        return JsonResponse(data or {"found": False})
    elif enquiry_number:
        data = zoho_lookup.lookup_by_enquiry(enquiry_number)
        return JsonResponse(data or {"found": False})

    return JsonResponse({"error": "Provide job_no or enquiry_number"}, status=400)
