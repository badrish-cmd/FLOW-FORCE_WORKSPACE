from django.db import migrations


SEEDED_TRACKER_NAMES = [
    "Operations Task Tracker",
    "Engineering Task Tracker",
    "Procurement Task Tracker",
    "Sales Task Tracker",
    "HR Task Tracker",
    "Finance Task Tracker",
]


def remove_seeded_trackers(apps, schema_editor):
    Tracker = apps.get_model("task_tracker", "Tracker")
    Tracker.objects.filter(name__in=SEEDED_TRACKER_NAMES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("task_tracker", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(remove_seeded_trackers, migrations.RunPython.noop),
    ]