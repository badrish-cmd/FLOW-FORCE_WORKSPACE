# Generated for updating existing general table columns

from django.db import migrations


def update_general_columns(apps, schema_editor):
    Column = apps.get_model('tables', 'Column')
    Column.objects.filter(table__job_type='GENERAL').update(is_system_column=False, is_mandatory=False)

def reverse_general_columns(apps, schema_editor):
    Column = apps.get_model('tables', 'Column')
    Column.objects.filter(table__job_type='GENERAL').update(is_system_column=True, is_mandatory=True)

class Migration(migrations.Migration):

    dependencies = [
        ('tables', '0013_cellvalue_tables_cell_column__407ab4_idx'),
    ]

    operations = [
        migrations.RunPython(update_general_columns, reverse_general_columns),
    ]
