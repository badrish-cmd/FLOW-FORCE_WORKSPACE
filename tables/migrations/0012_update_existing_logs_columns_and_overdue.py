from django.db import migrations
from django.utils import timezone
from datetime import datetime

def update_logs_columns_and_overdue(apps, schema_editor):
    Table = apps.get_model('tables', 'Table')
    Column = apps.get_model('tables', 'Column')
    Row = apps.get_model('tables', 'Row')
    CellValue = apps.get_model('tables', 'CellValue')

    today = timezone.localdate()
    logs_tables = Table.objects.filter(job_type='LOGS')

    for table in logs_tables:
        overdue_col = Column.objects.filter(table=table, name__iexact='DAYS_OVERDUE').first()
        if not overdue_col:
            overdue_col = Column.objects.create(
                table=table,
                name='DAYS_OVERDUE',
                data_type='NUMBER',
                is_mandatory=False,
                is_system_column=True,
                position=4
            )

        rows = Row.objects.filter(table=table, is_archived=False)
        for row in rows:
            cols = {col.name.upper(): col for col in Column.objects.filter(table=table)}
            issue_col = cols.get('ISSUE_DATE') or cols.get('DATE')
            return_col = cols.get('RETURN_DATE') or cols.get('DUE_DATE')
            status_col = cols.get('STATUS')

            issue_cell = CellValue.objects.filter(row=row, column=issue_col).first() if issue_col else None
            return_cell = CellValue.objects.filter(row=row, column=return_col).first() if return_col else None
            status_cell = CellValue.objects.filter(row=row, column=status_col).first() if status_col else None

            issue_date = None
            if issue_cell and issue_cell.value:
                try:
                    issue_date = datetime.strptime(str(issue_cell.value).split('T')[0], '%Y-%m-%d').date()
                except ValueError:
                    pass

            return_date = None
            if return_cell and return_cell.value:
                try:
                    return_date = datetime.strptime(str(return_cell.value).split('T')[0], '%Y-%m-%d').date()
                except ValueError:
                    pass

            if not return_date and issue_date:
                return_date = issue_date
                if return_col:
                    CellValue.objects.update_or_create(
                        row=row, column=return_col,
                        defaults={'value': return_date.isoformat()}
                    )

            status_val = str(status_cell.value).strip() if (status_cell and status_cell.value) else 'Not Returned'
            is_returned = status_val.upper() in ['RETURNED', 'COMPLETED']

            days_overdue = 0
            if is_returned:
                ref_date = row.updated_at.date() if row.updated_at else today
                if return_date and ref_date > return_date:
                    days_overdue = (ref_date - return_date).days
            else:
                if return_date and today > return_date:
                    days_overdue = (today - return_date).days

            CellValue.objects.update_or_create(
                row=row,
                column=overdue_col,
                defaults={'value': days_overdue}
            )

def reverse_logs_columns_and_overdue(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('tables', '0011_alter_table_job_type'),
    ]

    operations = [
        migrations.RunPython(update_logs_columns_and_overdue, reverse_logs_columns_and_overdue),
    ]
