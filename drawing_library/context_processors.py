from .permissions import has_library_access, is_admin_or_superadmin


def drawing_library_context(request):
    if not request.user.is_authenticated:
        return {
            "user_has_drawing_access": False,
            "user_is_drawing_admin": False,
        }

    return {
        "user_has_drawing_access": has_library_access(request.user),
        "user_is_drawing_admin": is_admin_or_superadmin(request.user),
    }
