from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("task_tracker", "0002_remove_seeded_trackers"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="TaskRow",
            new_name="TrackerRow",
        ),
        migrations.RenameModel(
            old_name="TaskCell",
            new_name="TrackerCell",
        ),
    ]