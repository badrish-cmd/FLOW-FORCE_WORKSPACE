"""
WebSocket consumers for the tables application.
Provides table-scoped real-time event connectivity with strict authentication
and authorization based on has_table_access(user, table, 'VIEW').
"""

import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Table
from .permissions import has_table_access


class TableEventConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer scoped to a specific table.
    Enforces authentication and VIEW authorization before accepting connections
    and joining the table group ('table_<table_id>').
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.table_id = None
        self.room_group_name = None

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        table_id_param = self.scope.get("url_route", {}).get("kwargs", {}).get("table_id")
        try:
            self.table_id = int(table_id_param)
        except (ValueError, TypeError):
            await self.close()
            return

        has_access = await self._check_table_access(user, self.table_id)
        if not has_access:
            await self.close()
            return

        self.room_group_name = f"table_{self.table_id}"
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )
        await self.accept()

    async def disconnect(self, close_code):
        if self.room_group_name:
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

    @database_sync_to_async
    def _check_table_access(self, user, table_id):
        try:
            table = Table.objects.select_related("created_by", "department").get(pk=table_id)
        except Table.DoesNotExist:
            return False
        return has_table_access(user, table, "VIEW")

    async def table_event(self, event):
        """
        Receives table group messages dispatched by broadcast publishers.
        Forwards the event payload to the connected WebSocket client.
        """
        payload = event.get("data", event.get("payload"))
        if payload is None:
            payload = {k: v for k, v in event.items() if k != "type"}
        if isinstance(payload, str):
            await self.send(text_data=payload)
        else:
            await self.send(text_data=json.dumps(payload))
