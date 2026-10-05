"""
Table Duplication Service.
Encapsulates all business logic for duplicating a table, its metadata, columns,
access rules, rows, cells, and associated tasks while safely suppressing
unintended background email notifications.
"""
import logging
from django.db import transaction

from tables.models import Table, Column, Row, CellValue, TableAccess
from tasks.models import Task

logger = logging.getLogger(__name__)


class TableDuplicateError(Exception):
    """Raised when an error occurs during table duplication."""
    pass


class TableDuplicateService:
    @staticmethod
    def duplicate_table(table: Table, user) -> Table:
        """
        Duplicates a table along with its columns, access rules, rows, cells, and tasks.

        Parameters:
            table (Table): The source table to clone.
            user (User): The user performing the duplication (assigned as created_by/updated_by).

        Returns:
            Table: The newly created duplicate table instance.

        Raises:
            Exception: If any step fails (rolls back atomically).
        """
        with transaction.atomic():
            # 1. Clone table metadata
            new_table = Table.objects.create(
                name=f"Copy of {table.name}",
                description=table.description,
                created_by=user,
                department=table.department,
                job_type=table.job_type,
            )

            column_mapping = {}

            # 2. Match system columns by name and copy options, position, is_mandatory, etc.
            # Note: Table.save() auto-creates system columns for new_table
            for old_col in table.columns.filter(is_system_column=True):
                new_col = new_table.columns.filter(name=old_col.name).first()
                if new_col:
                    new_col.options = old_col.options
                    new_col.position = old_col.position
                    new_col.is_mandatory = old_col.is_mandatory
                    new_col.save()
                    column_mapping[old_col.id] = new_col

            # 3. Clone custom columns (excluding system columns as they are auto-created in save())
            for old_col in table.columns.filter(is_system_column=False):
                existing_col = new_table.columns.filter(name=old_col.name).first()
                if existing_col:
                    existing_col.options = old_col.options
                    existing_col.position = old_col.position
                    existing_col.is_mandatory = old_col.is_mandatory
                    existing_col.save()
                    column_mapping[old_col.id] = existing_col
                else:
                    new_col = Column.objects.create(
                        table=new_table,
                        name=old_col.name,
                        data_type=old_col.data_type,
                        is_mandatory=old_col.is_mandatory,
                        is_system_column=False,
                        position=old_col.position,
                        options=old_col.options,
                    )
                    column_mapping[old_col.id] = new_col

            # 4. Clone TableAccess rules
            for access in table.access_rules.all():
                TableAccess.objects.create(
                    table=new_table,
                    user=access.user,
                    department=access.department,
                    access_level=access.access_level,
                )

            # 5. Clone Rows, CellValues and Tasks
            for old_row in table.rows.all():
                new_row = Row.objects.create(
                    table=new_table,
                    created_by=user,
                    is_archived=old_row.is_archived,
                )

                # Copy cells
                for old_cell in old_row.cells.all():
                    new_col = column_mapping.get(old_cell.column_id)
                    if new_col:
                        CellValue.objects.create(
                            row=new_row,
                            column=new_col,
                            value=old_cell.value,
                            updated_by=user,
                        )

                # Copy Task if it exists
                if hasattr(old_row, "task"):
                    old_task = old_row.task
                    new_task = Task(
                        row=new_row,
                        assigned_by=old_task.assigned_by,
                        status=old_task.status,
                        due_date=old_task.due_date,
                        priority=old_task.priority,
                        initial_mail_sent=old_task.initial_mail_sent,
                        alert_mail_sent=old_task.alert_mail_sent,
                        last_escalation_level=old_task.last_escalation_level,
                        last_escalation_at=old_task.last_escalation_at,
                    )
                    # CRITICAL SIGNAL SAFETY: Prevent email alert triggers on duplicate
                    new_task._skip_sync_signals = True
                    new_task._skip_assignment_signal = True
                    new_task.save()

                    if old_task.assigned_to.exists():
                        new_task._skip_assignment_signal = True
                        new_task.assigned_to.set(old_task.assigned_to.all())

            return new_table
