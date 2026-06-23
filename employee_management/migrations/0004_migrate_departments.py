# Generated migration to handle CharField to ForeignKey conversion for departments

from django.db import migrations
from django.utils.text import slugify


def migrate_departments_forward(apps, schema_editor):
    """
    Convert department CharField to ForeignKey.
    Creates Department objects from existing department strings.
    """
    Department = apps.get_model('employee_management', 'Department')
    EmployeeUser = apps.get_model('auth_app', 'EmployeeUser')
    
    # First, remove empty string values (set to None)
    EmployeeUser.objects.filter(department='').update(department=None)
    
    # Get all unique department values
    unique_departments = (
        EmployeeUser.objects
        .exclude(department__isnull=True)
        .values_list('department', flat=True)
        .distinct()
    )
    
    # Create Department objects from existing unique departments
    dept_map = {}
    for dept_name in unique_departments:
        dept_name = str(dept_name).strip()
        if dept_name:
            dept, created = Department.objects.get_or_create(
                name=dept_name,
                defaults={'slug': slugify(dept_name)}
            )
            dept_map[dept_name] = dept
    
    # Create default departments if not already present
    default_departments = [
        'Operations', 'Engineering', 'Procurement', 'Sales', 'HR', 'Finance'
    ]
    for dept_name in default_departments:
        if dept_name not in dept_map:
            dept, created = Department.objects.get_or_create(
                name=dept_name,
                defaults={'slug': slugify(dept_name)}
            )
            dept_map[dept_name] = dept


def migrate_departments_backward(apps, schema_editor):
    """
    Reverse: Convert ForeignKey back to CharField.
    This is a one-way operation in practice.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('employee_management', '0003_department'),
    ]

    operations = [
        migrations.RunPython(migrate_departments_forward, migrate_departments_backward),
    ]

