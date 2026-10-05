from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import HttpResponseForbidden, JsonResponse
from django.contrib import messages
from django.utils import timezone
from datetime import datetime

from .models import Announcement, AnnouncementRead


def is_admin_user(user):
    return user.is_authenticated and (user.role in ["ADMIN", "SUPER_ADMIN"] or user.is_superuser)


@login_required
def announcements_list_view(request):
    """
    Public archive of published system updates / announcements for all employees.
    Unpublished/draft announcements are strictly excluded.
    """
    announcements = Announcement.objects.filter(is_published=True).order_by("-published_at", "-id")

    read_ids = set(
        AnnouncementRead.objects.filter(employee=request.user).values_list("announcement_id", flat=True)
    )

    announcement_items = []
    for item in announcements:
        announcement_items.append({
            "announcement": item,
            "is_read": item.id in read_ids,
        })

    context = {
        "announcements": announcement_items,
        "total_published": len(announcement_items),
        "is_admin": is_admin_user(request.user),
    }
    return render(request, "announcements/list.html", context)


@login_required
@require_POST
def announcement_acknowledge_view(request, announcement_id):
    """
    Acknowledges an active published announcement for the current employee ("Got it").
    Ensures that this employee will not see the announcement popup again.
    """
    announcement = get_object_or_404(Announcement, id=announcement_id, is_published=True)

    AnnouncementRead.objects.get_or_create(
        announcement=announcement,
        employee=request.user,
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"status": "success", "announcement_id": announcement.id})

    return redirect(request.META.get("HTTP_REFERER", "/"))


@login_required
def admin_announcements_view(request):
    """
    Management dashboard for announcements.
    Only ADMIN and SUPER_ADMIN users can access.
    """
    if not is_admin_user(request.user):
        return HttpResponseForbidden("Only administrators can manage announcements.")

    tab = request.GET.get("tab", "all").strip().lower()
    base_qs = Announcement.objects.all().select_related("created_by")

    total_count = base_qs.count()
    published_count = base_qs.filter(is_published=True).count()
    draft_count = base_qs.filter(is_published=False).count()

    if tab == "published":
        announcements = base_qs.filter(is_published=True)
    elif tab == "drafts":
        announcements = base_qs.filter(is_published=False)
    else:
        tab = "all"
        announcements = base_qs

    # Annotate read counts
    announcements_with_reads = []
    for item in announcements:
        announcements_with_reads.append({
            "item": item,
            "read_count": item.reads.count(),
        })

    context = {
        "announcements": announcements_with_reads,
        "tab": tab,
        "total_count": total_count,
        "published_count": published_count,
        "draft_count": draft_count,
        "category_choices": Announcement.CATEGORY_CHOICES,
    }
    return render(request, "announcements/manage.html", context)


@login_required
@require_POST
def admin_announcement_create_view(request):
    """
    Create a new announcement (draft or published).
    """
    if not is_admin_user(request.user):
        return HttpResponseForbidden("Only administrators can create announcements.")

    title = request.POST.get("title", "").strip()
    category = request.POST.get("category", "IMPROVEMENT").strip()
    content = request.POST.get("content", "").strip()
    bullet_points = request.POST.get("bullet_points", "").strip()
    is_published = request.POST.get("is_published") in ["on", "true", "1", "True"]

    if not title or not content:
        messages.error(request, "Title and content are required.")
        return redirect("announcements_manage")

    valid_categories = [c[0] for c in Announcement.CATEGORY_CHOICES]
    if category not in valid_categories:
        category = "IMPROVEMENT"

    published_at_str = request.POST.get("published_at", "").strip()
    published_at = None
    if published_at_str:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(published_at_str, fmt)
                published_at = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
                break
            except ValueError:
                pass

    if is_published and not published_at:
        published_at = timezone.now()

    Announcement.objects.create(
        title=title,
        category=category,
        content=content,
        bullet_points=bullet_points,
        is_published=is_published,
        published_at=published_at,
        created_by=request.user,
    )
    messages.success(request, f"Announcement '{title}' created successfully.")
    return redirect("announcements_manage")


@login_required
@require_POST
def admin_announcement_edit_view(request, announcement_id):
    """
    Edit an existing announcement.
    """
    if not is_admin_user(request.user):
        return HttpResponseForbidden("Only administrators can edit announcements.")

    announcement = get_object_or_404(Announcement, id=announcement_id)

    title = request.POST.get("title", "").strip()
    category = request.POST.get("category", "").strip()
    content = request.POST.get("content", "").strip()
    bullet_points = request.POST.get("bullet_points", "").strip()
    is_published = request.POST.get("is_published") in ["on", "true", "1", "True"]

    if not title or not content:
        messages.error(request, "Title and content are required.")
        return redirect("announcements_manage")

    valid_categories = [c[0] for c in Announcement.CATEGORY_CHOICES]
    if category in valid_categories:
        announcement.category = category

    announcement.title = title
    announcement.content = content
    announcement.bullet_points = bullet_points
    announcement.is_published = is_published

    published_at_str = request.POST.get("published_at", "").strip()
    if published_at_str:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(published_at_str, fmt)
                announcement.published_at = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
                break
            except ValueError:
                pass
    elif is_published and not announcement.published_at:
        announcement.published_at = timezone.now()

    announcement.save()
    messages.success(request, f"Announcement '{title}' updated successfully.")
    return redirect("announcements_manage")


@login_required
@require_POST
def admin_announcement_toggle_publish_view(request, announcement_id):
    """
    Toggle an announcement between published and draft state.
    """
    if not is_admin_user(request.user):
        return HttpResponseForbidden("Only administrators can toggle publication status.")

    announcement = get_object_or_404(Announcement, id=announcement_id)
    announcement.is_published = not announcement.is_published
    if announcement.is_published and not announcement.published_at:
        announcement.published_at = timezone.now()
    announcement.save(update_fields=["is_published", "published_at", "updated_at"])

    state_str = "published" if announcement.is_published else "unpublished (saved as draft)"
    messages.success(request, f"Announcement '{announcement.title}' is now {state_str}.")
    return redirect("announcements_manage")


@login_required
@require_POST
def admin_announcement_delete_view(request, announcement_id):
    """
    Delete an announcement.
    """
    if not is_admin_user(request.user):
        return HttpResponseForbidden("Only administrators can delete announcements.")

    announcement = get_object_or_404(Announcement, id=announcement_id)
    title = announcement.title
    announcement.delete()
    messages.success(request, f"Announcement '{title}' has been deleted.")
    return redirect("announcements_manage")
