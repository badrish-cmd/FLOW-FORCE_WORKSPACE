"""
Real-time Table and Task Event Broadcaster.
Publishes domain events to table-scoped Django Channels groups ('table_<table_id>').
All broadcasts are strictly deferred until the current database transaction commits
via transaction.on_commit to ensure transaction safety and prevent phantom events on rollback.
"""

import logging
from typing import Optional, Dict, Any, List
from django.db import transaction
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def _serialize_user(user) -> Optional[Dict[str, Any]]:
    """
    Safely serializes minimal non-sensitive user identity.
    Does not expose passwords, tokens, roles, or private data.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    name = getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)
    return {
        "id": getattr(user, "id", None),
        "name": name,
    }


class TableEventBroadcaster:
    """
    Dedicated broadcaster for real-time table and task events.
    Guarantees:
    1. Transaction safety: Only broadcasts after the database transaction successfully commits.
    2. Resiliency: Group send errors are logged and never undo/fail database mutations.
    3. Group isolation: Dispatches exclusively to 'table_<table_id>'.
    4. Safe serialization: Never exposes sensitive credentials or private user data.
    """

    @classmethod
    def _send_to_group(cls, table_id: int, payload: Dict[str, Any]) -> None:
        """
        Internal dispatcher that sends payload to Channels group.
        Protected by try-except so communication failures never impact database operations.
        """
        try:
            channel_layer = get_channel_layer()
            if not channel_layer:
                logger.warning("No channel layer configured; skipping broadcast for table_%s", table_id)
                return

            group_name = f"table_{table_id}"
            message = {
                "type": "table.event",
                "data": payload,
            }
            async_to_sync(channel_layer.group_send)(group_name, message)
        except Exception as e:
            logger.exception("Failed to broadcast table event to %s: %s", f"table_{table_id}", e)

    @classmethod
    def broadcast_event(cls, table_id: int, event_type: str, data: Optional[Dict[str, Any]] = None, user=None) -> None:
        """
        Enqueues an event to be broadcast to group table_<table_id> ON TRANSACTION COMMIT.
        If called outside an active atomic transaction, executes immediately.
        """
        if not table_id:
            return

        payload = {
            "type": "table.event",
            "event": event_type,
            "table_id": int(table_id),
            "timestamp": timezone.now().isoformat(),
        }
        if user:
            payload["updated_by"] = _serialize_user(user)
        if data:
            payload.update(data)

        # Defer broadcast until transaction commit
        transaction.on_commit(lambda: cls._send_to_group(int(table_id), payload))

    # -------------------------------------------------------------------------
    # Row Events
    # -------------------------------------------------------------------------

    @classmethod
    def broadcast_row_created(cls, table_id: int, row_id: int, task_id: Optional[int] = None, user=None, extra: Optional[Dict[str, Any]] = None) -> None:
        data = {"row_id": row_id}
        if task_id:
            data["task_id"] = task_id
        if extra:
            data.update(extra)
        cls.broadcast_event(table_id=table_id, event_type="row_created", data=data, user=user)

    @classmethod
    def broadcast_row_updated(cls, table_id: int, row_id: int, updated_columns: Optional[List[str]] = None, task_id: Optional[int] = None, user=None) -> None:
        data = {"row_id": row_id}
        if updated_columns:
            data["updated_columns"] = updated_columns
        if task_id:
            data["task_id"] = task_id
        cls.broadcast_event(table_id=table_id, event_type="row_updated", data=data, user=user)

    @classmethod
    def broadcast_row_deleted(cls, table_id: int, row_id: int, task_id: Optional[int] = None, user=None) -> None:
        data = {"row_id": row_id}
        if task_id:
            data["task_id"] = task_id
        cls.broadcast_event(table_id=table_id, event_type="row_deleted", data=data, user=user)

    @classmethod
    def broadcast_cell_updated(cls, table_id: int, row_id: int, column_id: int, column_name: str, value: Any, task_id: Optional[int] = None, user=None) -> None:
        data = {
            "row_id": row_id,
            "column_id": column_id,
            "column_name": column_name,
            "value": value,
        }
        if task_id:
            data["task_id"] = task_id
        cls.broadcast_event(table_id=table_id, event_type="cell_updated", data=data, user=user)

    # -------------------------------------------------------------------------
    # Task Events
    # -------------------------------------------------------------------------

    @classmethod
    def broadcast_task_created(cls, table_id: int, task_id: int, row_id: Optional[int] = None, user=None, extra: Optional[Dict[str, Any]] = None) -> None:
        data = {"task_id": task_id}
        if row_id:
            data["row_id"] = row_id
        if extra:
            data.update(extra)
        cls.broadcast_event(table_id=table_id, event_type="task_created", data=data, user=user)

    @classmethod
    def broadcast_task_updated(cls, table_id: int, task_id: int, row_id: Optional[int] = None, user=None, changes: Optional[Dict[str, Any]] = None) -> None:
        data = {"task_id": task_id}
        if row_id:
            data["row_id"] = row_id
        if changes:
            data["changes"] = changes
        cls.broadcast_event(table_id=table_id, event_type="task_updated", data=data, user=user)

    @classmethod
    def broadcast_task_status_changed(cls, table_id: int, task_id: int, row_id: Optional[int] = None, old_status: Optional[str] = None, new_status: Optional[str] = None, user=None) -> None:
        data = {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
        }
        if row_id:
            data["row_id"] = row_id
        cls.broadcast_event(table_id=table_id, event_type="task_status_changed", data=data, user=user)

    @classmethod
    def broadcast_task_reassigned(cls, table_id: int, task_id: int, row_id: Optional[int] = None, assignees: Optional[List[Dict[str, Any]]] = None, user=None) -> None:
        data = {
            "task_id": task_id,
            "assigned_to": assignees or [],
        }
        if row_id:
            data["row_id"] = row_id
        cls.broadcast_event(table_id=table_id, event_type="task_reassigned", data=data, user=user)

    @classmethod
    def broadcast_task_deleted(cls, table_id: int, task_id: int, row_id: Optional[int] = None, user=None) -> None:
        data = {"task_id": task_id}
        if row_id:
            data["row_id"] = row_id
        cls.broadcast_event(table_id=table_id, event_type="task_deleted", data=data, user=user)

    # -------------------------------------------------------------------------
    # Bulk / Batch Events (To prevent event storms)
    # -------------------------------------------------------------------------

    @classmethod
    def broadcast_rows_updated(cls, table_id: int, count: int, field: Optional[str] = None, value: Any = None, row_ids: Optional[List[int]] = None, user=None) -> None:
        data = {"count": count}
        if field:
            data["field"] = field
        if value is not None:
            data["value"] = value
        if row_ids:
            data["row_ids"] = row_ids
        cls.broadcast_event(table_id=table_id, event_type="rows_updated", data=data, user=user)

    @classmethod
    def broadcast_rows_deleted(cls, table_id: int, count: int, row_ids: Optional[List[int]] = None, user=None) -> None:
        data = {"count": count}
        if row_ids:
            data["row_ids"] = row_ids
        cls.broadcast_event(table_id=table_id, event_type="rows_deleted", data=data, user=user)

    @classmethod
    def broadcast_table_import_completed(cls, table_id: int, imported_rows: int, user=None) -> None:
        data = {"imported_rows": imported_rows}
        cls.broadcast_event(table_id=table_id, event_type="table_import_completed", data=data, user=user)
