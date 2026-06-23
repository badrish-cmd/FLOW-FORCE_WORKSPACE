from rest_framework import permissions
from .permissions import has_table_access

class HasTableAccess(permissions.BasePermission):
    """
    DRF permission class to verify if a user has access to a Table.
    """
    def has_object_permission(self, request, view, obj):
        # Determine required access level based on the HTTP method
        if request.method in permissions.SAFE_METHODS:
            required_level = "VIEW"
        else:
            required_level = "EDIT"
            
        # Check if the object is a Table or has a reference to table
        if hasattr(obj, "table"):
            table = obj.table
        else:
            table = obj

        return has_table_access(request.user, table, required_level)
