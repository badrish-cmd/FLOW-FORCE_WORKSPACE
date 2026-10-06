"""
WebSocket URL routing for tables app.
"""

from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/tables/(?P<table_id>\d+)/$", consumers.TableEventConsumer.as_asgi()),
]
