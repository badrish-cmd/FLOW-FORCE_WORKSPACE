from functools import wraps
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.contrib import messages
from django.db.models import Q
from .models import EngineeringDrawing, DrawingAccess


def is_admin_or_superadmin(user):
    """Check if the user is a Super Admin or Admin."""
    if not user.is_authenticated:
        return False
    return getattr(user, "is_superuser", False) or getattr(user, "role", "") in ["SUPER_ADMIN", "ADMIN"]


def can_manage_drawing_access(user):
    """Only Super Admin and Admin can assign or revoke access to other users."""
    return is_admin_or_superadmin(user)


def has_library_access(user):
    """
    Check if the user has access to view the drawing library in general.
    - Super Admin / Admin: Always True
    - Other users: True ONLY if granted global or drawing-specific DrawingAccess.
    """
    if not user.is_authenticated:
        return False
    if is_admin_or_superadmin(user):
        return True

    return DrawingAccess.objects.filter(user=user).exists()


def has_library_edit_access(user):
    """
    Check if user has full edit/management access to the whole library.
    - Super Admin / Admin: Always True
    - Users with global DrawingAccess with access_level="EDIT": True
    """
    if not user.is_authenticated:
        return False
    if is_admin_or_superadmin(user):
        return True
    return DrawingAccess.objects.filter(drawing__isnull=True, user=user, access_level="EDIT").exists()


def can_create_drawing(user, parent_drawing=None):
    """
    Check if user has permission to create drawings.
    - Admin / Super Admin: Always True
    - If parent_drawing is None (new Level 1 Project): User must have global library edit access.
    - If parent_drawing is provided (Level 2 Child / Level 3 Grandchild):
      User must have global library edit access OR EDIT access on the parent drawing tree.
    """
    if not user.is_authenticated:
        return False
    if is_admin_or_superadmin(user):
        return True
    if has_library_edit_access(user):
        return True
    if parent_drawing and has_drawing_access(user, parent_drawing, required_level="EDIT"):
        return True
    return False


def get_accessible_drawings(user):
    """
    Return queryset of drawings accessible to the user.
    """
    if not user.is_authenticated:
        return EngineeringDrawing.objects.none()

    if is_admin_or_superadmin(user):
        return EngineeringDrawing.objects.all()

    # Check if user has global library access
    if DrawingAccess.objects.filter(drawing__isnull=True, user=user).exists():
        return EngineeringDrawing.objects.all()

    # Drawing-specific access (including children/descendants)
    accessible_drawing_ids = set(DrawingAccess.objects.filter(drawing__isnull=False, user=user).values_list("drawing_id", flat=True))
    all_drawings = list(EngineeringDrawing.objects.all())
    expanded_ids = set(accessible_drawing_ids)
    changed = True
    while changed:
        changed = False
        for d in all_drawings:
            if d.parent_id in expanded_ids and d.id not in expanded_ids:
                expanded_ids.add(d.id)
                changed = True
    return EngineeringDrawing.objects.filter(id__in=expanded_ids)


def has_drawing_access(user, drawing, required_level="VIEW"):
    """
    Check if user has access to a specific drawing at the required level ('VIEW' or 'EDIT').
    Inherits access from parent/root drawings.
    """
    if not user.is_authenticated:
        return False

    if is_admin_or_superadmin(user):
        return True

    # Check global library access
    global_q = Q(drawing__isnull=True, user=user)
    if required_level == "EDIT":
        global_q &= Q(access_level="EDIT")
    if DrawingAccess.objects.filter(global_q).exists():
        return True

    # Check drawing specific access (direct or inherited from ancestors)
    target_ids = [drawing.id]
    curr = drawing
    while curr.parent_id:
        target_ids.append(curr.parent_id)
        curr = curr.parent

    drawing_q = Q(drawing_id__in=target_ids, user=user)
    if required_level == "EDIT":
        drawing_q &= Q(access_level="EDIT")

    return DrawingAccess.objects.filter(drawing_q).exists()


def drawing_library_access_required(view_func):
    """View decorator to ensure user has access to the drawing library."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not has_library_access(request.user):
            messages.error(request, "Access Denied: You do not have permission to access the Engineering Drawing Library.")
            return redirect("dashboard:dashboard" if "dashboard" in str(request.resolver_match) else "/")
        return view_func(request, *args, **kwargs)
    return wrapper


def admin_or_superadmin_required(view_func):
    """View decorator restricting access strictly to Admin / Super Admin."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not is_admin_or_superadmin(request.user):
            messages.error(request, "Access Denied: Only Admins and Super Admins can perform this action.")
            return redirect("drawings:drawing_list")
        return view_func(request, *args, **kwargs)
    return wrapper
