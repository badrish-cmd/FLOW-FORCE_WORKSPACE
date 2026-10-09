import os, time, cProfile, pstats
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flowforce.settings")
import django; django.setup()

from tables.models import Table, Row
from tables.serializers import RowSerializer

t = Table.objects.filter(job_type="LIST_PID").first()
if not t:
    t = Table.objects.first()

if t:
    rows = list(Row.objects.filter(table=t, is_archived=False).select_related(
        'created_by', 'task', 'task__assigned_by'
    ).prefetch_related(
        'cells__column', 'cells__updated_by', 'task__assigned_to'
    )[:50])

    print(f"Loaded {len(rows)} rows for profiling")
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(5):
        data = RowSerializer(rows, many=True).data
    pr.disable()

    ps = pstats.Stats(pr).sort_stats('cumulative')
    ps.print_stats(30)
else:
    print("No table found to profile")
