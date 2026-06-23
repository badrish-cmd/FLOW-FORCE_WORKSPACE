# Generated migration to complete department FK migration

from django.db import migrations, models
import django.db.models.deletion


def migrate_department_data(apps, schema_editor):
    """
    Populate department_fk from department string field,
    then we can remove the old field
    """
    Department = apps.get_model('employee_management', 'Department')
    Tracker = apps.get_model('task_tracker', 'Tracker')
    
    # Map department string values to FK IDs
    for tracker in Tracker.objects.all():
        if tracker.department and tracker.department.strip():
            dept_name = str(tracker.department).strip()
            try:
                dept = Department.objects.get(name=dept_name)
                tracker.department_fk = dept
                tracker.save(update_fields=['department_fk'])
            except Department.DoesNotExist:
                # Should not happen if previous migration worked, but just in case
                dept = Department.objects.create(name=dept_name)
                tracker.department_fk = dept
                tracker.save(update_fields=['department_fk'])


class Migration(migrations.Migration):

    dependencies = [
        ('task_tracker', '0006_alter_tracker_department_and_more'),
    ]

    operations = [
        migrations.RunPython(migrate_department_data, migrations.RunPython.noop),
        # Now remove the old field
        migrations.RemoveField(
            model_name='tracker',
            name='department',
        ),
        # Rename the new field to department
        migrations.RenameField(
            model_name='tracker',
            old_name='department_fk',
            new_name='department',
        ),
        # Alter the field properties (update help_text and related_name)
        migrations.AlterField(
            model_name='tracker',
            name='department',
            field=models.ForeignKey(help_text='Department this tracker belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='trackers', to='employee_management.department'),
        ),
        migrations.AlterUniqueTogether(
            name='tracker',
            unique_together={('department', 'name')},
        ),
    ]
