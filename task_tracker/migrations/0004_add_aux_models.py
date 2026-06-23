from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("task_tracker", "0003_rename_task_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaskComment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content", models.TextField()),
                ("mentions", models.JSONField(default=list, blank=True)),
                ("internal", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "row",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="task_tracker.trackerrow"),
                ),
                (
                    "parent",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="replies", to="task_tracker.taskcomment", null=True, blank=True),
                ),
                (
                    "created_by",
                    models.ForeignKey(on_delete=django.db.models.deletion.SET_NULL, related_name="comments_created", to=settings.AUTH_USER_MODEL, null=True, blank=True),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="TaskAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to="task_attachments/")),
                ("original_name", models.CharField(max_length=255, blank=True)),
                ("content_type", models.CharField(max_length=120, blank=True)),
                ("size", models.PositiveIntegerField(null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "row",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attachments", to="task_tracker.trackerrow"),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(on_delete=django.db.models.deletion.SET_NULL, related_name="attachments_uploaded", to=settings.AUTH_USER_MODEL, null=True, blank=True),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="TaskHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=120)),
                ("field_name", models.CharField(max_length=120, blank=True)),
                ("old_value", models.TextField(blank=True)),
                ("new_value", models.TextField(blank=True)),
                ("metadata", models.JSONField(default=dict, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "row",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="history", to="task_tracker.trackerrow"),
                ),
                (
                    "changed_by",
                    models.ForeignKey(on_delete=django.db.models.deletion.SET_NULL, related_name="history_actions", to=settings.AUTH_USER_MODEL, null=True, blank=True),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="Notification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("notif_type", models.CharField(max_length=40, choices=[("ASSIGNMENT", "ASSIGNMENT"), ("ALERT", "ALERT"), ("ESCALATION", "ESCALATION")])),
                ("payload", models.JSONField(default=dict, blank=True)),
                ("sent_at", models.DateTimeField(null=True, blank=True)),
                ("read", models.BooleanField(default=False)),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notifications", to=settings.AUTH_USER_MODEL),
                ),
                (
                    "row",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notifications", to="task_tracker.trackerrow", null=True, blank=True),
                ),
            ],
            options={"ordering": ["-sent_at", "-id"]},
        ),
    ]
