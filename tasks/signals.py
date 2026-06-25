from django.db.models.signals import post_save, post_init, m2m_changed, post_delete
from django.dispatch import receiver
from django.db.models import Q
from .models import Task
from .tasks import send_initial_mail, send_review_request_mail, send_approval_status_mail, update_task_row_mail_columns

def sync_task_assignments(task):
    """
    Sync task assignments to all non-admin employees who have access to the table.
    """
    try:
        from auth_app.models import EmployeeUser
        from tables.models import TableAccess

        table = task.row.table
        
        # Non-admin employees who can access this table
        employees = EmployeeUser.objects.filter(is_active=True).exclude(role__in=["ADMIN", "SUPER_ADMIN"])
        
        # Check TableAccess rules for the table
        access_rules = TableAccess.objects.filter(table=table)
        user_ids = access_rules.filter(user__isnull=False).values_list("user_id", flat=True)
        dept_ids = access_rules.filter(department__isnull=False).values_list("department_id", flat=True)
        
        q_filter = Q(id__in=user_ids)
        if dept_ids:
            q_filter |= Q(department_id__in=dept_ids)
            
        target_employees = employees.filter(q_filter).distinct()
        task.assigned_to.set(target_employees)
    except Exception:
        pass

@receiver(post_init, sender=Task)
def cache_task_status(sender, instance, **kwargs):
    """Cache the status when task is loaded from database to track changes."""
    instance._old_status = instance.status

@receiver(post_save, sender=Task)
def handle_task_save(sender, instance, created, **kwargs):
    """
    Initialize row columns on creation or update cell values when mail state changes.
    Also triggers review request/approval status emails when status changes.
    """
    if created:
        update_task_row_mail_columns(instance)
        sync_task_assignments(instance)
    else:
        # Email escalations on status changes are disabled as per request
        pass

@receiver(m2m_changed, sender=Task.assigned_to.through)
def handle_task_assignment(sender, instance, action, **kwargs):
    """
    Trigger INITIAL_MAIL when task is assigned/updated.
    """
    if action == "post_add":
        send_initial_mail.delay(instance.id)

# Import TableAccess dynamically/locally or at the end to prevent potential import loops
from tables.models import TableAccess

@receiver(post_save, sender=TableAccess)
def handle_table_access_save(sender, instance, **kwargs):
    for task in Task.objects.filter(row__table=instance.table):
        sync_task_assignments(task)

@receiver(post_delete, sender=TableAccess)
def handle_table_access_delete(sender, instance, **kwargs):
    for task in Task.objects.filter(row__table=instance.table):
        sync_task_assignments(task)

