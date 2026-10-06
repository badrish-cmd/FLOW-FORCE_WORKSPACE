"""
Table Delete Service.
Encapsulates all business logic for row deletions, bulk deletions,
column-filtered row deletions, and column value clearing while preserving
cascading behavior and invalidating table statistics cache.
"""
from typing import Optional, List
import logging
from django.db import transaction
from django.db.models import Q

from tables.models import Table, Column, Row, CellValue
from .statistics_service import TableStatisticsService

logger = logging.getLogger(__name__)


class DeleteError(Exception):
    """Base exception for delete operations."""
    pass


class DeleteValidationError(DeleteError):
    """Raised when validation fails for a delete operation."""
    pass


class DeletePermissionDeniedError(DeleteError):
    """Raised when user lacks permission to perform delete operation."""
    pass


class TableDeleteService:
    @staticmethod
    def delete_row(row: Row, user) -> None:
        """
        Deletes a single Row instance.
        Django's CASCADE removes associated CellValue and Task records.
        Row.delete() automatically invalidates table statistics cache.
        """
        table_id = row.table_id
        row.delete()
        TableStatisticsService.invalidate_cache(table_id)

    @staticmethod
    def bulk_delete_rows(table: Table, user, row_ids: Optional[List[int]] = None) -> int:
        """
        Deletes rows belonging to the given table.
        If row_ids is provided, deletes only matching rows.
        If row_ids is None, deletes all rows belonging to the table.

        Parameters:
            table (Table): The target table.
            user (User): The requesting user.
            row_ids (list, optional): List of row IDs to delete.

        Returns:
            int: The number of rows deleted.

        Raises:
            DeleteValidationError: If row_ids is provided but is not a list.
        """
        if row_ids is not None:
            if not isinstance(row_ids, list):
                raise DeleteValidationError("row_ids must be a list")
            rows = Row.objects.filter(table=table, id__in=row_ids)
        else:
            rows = Row.objects.filter(table=table)

        with transaction.atomic():
            count = rows.count()
            rows.delete()
            TableStatisticsService.invalidate_cache(table.id)

        return count

    @staticmethod
    def delete_rows_by_column(column: Column, user) -> int:
        """
        Deletes all rows in the column's table containing non-empty values for this column.
        Rejects system columns.

        Parameters:
            column (Column): The column to filter by.
            user (User): The requesting user.

        Returns:
            int: The number of rows deleted.

        Raises:
            DeleteValidationError: If column is a system column.
        """
        if column.is_system_column:
            raise DeleteValidationError("Cannot delete rows using system column filter")

        with transaction.atomic():
            rows = Row.objects.filter(
                table=column.table,
                cells__column=column
            ).exclude(
                Q(cells__value__isnull=True) | Q(cells__value="")
            )
            count = rows.count()
            rows.delete()
            TableStatisticsService.invalidate_cache(column.table_id)

        return count

    @staticmethod
    def clear_column_values(column: Column, user) -> None:
        """
        Clears (sets to None) all cell values in the given column.
        Rejects system columns.

        Parameters:
            column (Column): The column whose cell values to clear.
            user (User): The requesting user.

        Raises:
            DeleteValidationError: If column is a system column.
        """
        if column.is_system_column:
            raise DeleteValidationError("Cannot clear system columns")

        with transaction.atomic():
            CellValue.objects.filter(column=column).update(value=None)
            TableStatisticsService.invalidate_cache(column.table_id)
