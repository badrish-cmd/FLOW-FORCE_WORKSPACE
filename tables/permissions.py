from django.db.models import Q
from .models import Table, Column, TableAccess, ColumnAccess

def get_accessible_tables(user):
    """
    Get a queryset of all tables accessible by the given user based on role and sharing rules.
    """
    if not user.is_authenticated:
        return Table.objects.none()

    if user.role == "SUPER_ADMIN":
        return Table.objects.filter(is_active=True)

    # Base query for active tables
    qs = Table.objects.filter(is_active=True)

    # For ADMIN and DEPARTMENT_ADMIN
    if user.role in ["ADMIN", "DEPARTMENT_ADMIN"]:
        shared_filters = Q(user=user)
        if user.department:
            shared_filters |= Q(department=user.department)
        
        shared_table_ids = TableAccess.objects.filter(shared_filters).values_list("table_id", flat=True)
        
        # They can see tables they created, tables of their department, or tables shared with them/their department
        q_filter = Q(created_by=user) | Q(id__in=shared_table_ids)
        if user.department:
            q_filter |= Q(department=user.department)
            
        return qs.filter(q_filter).distinct()

    # For EMPLOYEE, they only see tables explicitly shared with them or their department, or created by them
    if user.role == "EMPLOYEE":
        shared_filters = Q(user=user)
        if user.department:
            shared_filters |= Q(department=user.department)
        
        shared_table_ids = TableAccess.objects.filter(shared_filters).values_list("table_id", flat=True)
        return qs.filter(Q(id__in=shared_table_ids) | Q(created_by=user)).distinct()

    # Fallback
    return Table.objects.none()

def has_table_access(user, table, required_level="VIEW"):
    """
    Check if a user has a specific level of access to a Table.
    required_level: 'VIEW', 'EDIT', or 'ADMIN'
    """
    if not user.is_authenticated:
        return False

    if user.role == "SUPER_ADMIN":
        return True

    # If the user is the owner/creator of the table
    if table.created_by == user:
        return True

    # Admin and Dept Admin have admin level access in their department
    if user.role in ["ADMIN", "DEPARTMENT_ADMIN"] and user.department and table.department == user.department:
        return True

    # Check explicit access rules
    access_filters = Q(table=table)
    user_q = Q(user=user)
    if user.department:
        user_q |= Q(department=user.department)
    
    access_rules = TableAccess.objects.filter(access_filters & user_q)
    if not access_rules.exists():
        return False

    # Check access levels hierarchy: ADMIN > EDIT > VIEW
    levels_hierarchy = {
        "VIEW": 1,
        "EDIT": 2,
        "ADMIN": 3
    }
    
    max_user_level = 0
    for rule in access_rules:
        level_value = levels_hierarchy.get(rule.access_level, 0)
        if level_value > max_user_level:
            max_user_level = level_value

    required_value = levels_hierarchy.get(required_level, 1)
    return max_user_level >= required_value

def get_column_access_level(user, column):
    """
    Check the permission level for a column for a user.
    Returns: 'HIDDEN', 'READ_ONLY', or 'EDITABLE'
    """
    if not user.is_authenticated:
        return "HIDDEN"

    if user.role == "SUPER_ADMIN":
        return "EDITABLE"

    # If the user is table creator or department admin for this table
    table = column.table
    if table.created_by == user:
        return "EDITABLE"

    if user.role in ["ADMIN", "DEPARTMENT_ADMIN"] and user.department and table.department == user.department:
        return "EDITABLE"

    # System columns default to read-only for employees or editable based on context
    # S_NO, DATE, DUE_DATE, TASK_NAME, INITIAL_MAIL, ALERT_MAIL
    # Let's check ColumnAccess
    try:
        access_rule = ColumnAccess.objects.get(column=column, user=user)
        return access_rule.access_level
    except ColumnAccess.DoesNotExist:
        # Default behavior:
        # System columns INITIAL_MAIL and ALERT_MAIL are always READ_ONLY for employees,
        # and S_NO is always READ_ONLY for everyone.
        if column.is_system_column:
            if column.name == "S_NO":
                return "READ_ONLY"
            if column.name in ["INITIAL_MAIL", "ALERT_MAIL"]:
                # Only admins can edit email status columns directly
                if user.role in ["SUPER_ADMIN", "ADMIN", "DEPARTMENT_ADMIN"]:
                    return "EDITABLE"
                return "READ_ONLY"
            # DUE_DATE, TASK_NAME, DATE: editable if they have edit permission on the table
            if has_table_access(user, table, "EDIT"):
                return "EDITABLE"
            return "READ_ONLY"

        # Non-system columns: editable if they have table EDIT access
        if has_table_access(user, table, "EDIT"):
            return "EDITABLE"
        return "READ_ONLY"
