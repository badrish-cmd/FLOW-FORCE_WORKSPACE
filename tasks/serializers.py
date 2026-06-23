from rest_framework import serializers
from .models import Task, TaskComment, ActivityLog, Notification, EmailLog
from tables.serializers import UserMinSerializer

class TaskMinSerializer(serializers.ModelSerializer):
    assigned_to_details = UserMinSerializer(source="assigned_to", many=True, read_only=True)
    assigned_by_detail = UserMinSerializer(source="assigned_by", read_only=True)

    class Meta:
        model = Task
        fields = ["id", "row", "assigned_by", "assigned_by_detail", "assigned_to", "assigned_to_details", "status", "due_date", "priority", "created_at", "updated_at"]

class TaskCommentSerializer(serializers.ModelSerializer):
    author_detail = UserMinSerializer(source="author", read_only=True)

    class Meta:
        model = TaskComment
        fields = ["id", "task", "author", "author_detail", "content", "is_internal_note", "created_at", "updated_at"]

class ActivityLogSerializer(serializers.ModelSerializer):
    user_detail = UserMinSerializer(source="user", read_only=True)

    class Meta:
        model = ActivityLog
        fields = ["id", "task", "action", "details", "user", "user_detail", "timestamp"]

class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "user", "task", "title", "description", "type", "is_read", "created_at", "updated_at"]

class EmailLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailLog
        fields = ["id", "recipient_email", "subject", "task", "email_type", "status", "error_msg", "created_at", "updated_at"]

class TaskSerializer(serializers.ModelSerializer):
    assigned_to_details = UserMinSerializer(source="assigned_to", many=True, read_only=True)
    assigned_by_detail = UserMinSerializer(source="assigned_by", read_only=True)
    comments = TaskCommentSerializer(many=True, read_only=True)
    activity_logs = ActivityLogSerializer(many=True, read_only=True)

    class Meta:
        model = Task
        fields = ["id", "row", "assigned_by", "assigned_by_detail", "assigned_to", "assigned_to_details", "status", "due_date", "priority", "created_at", "updated_at", "comments", "activity_logs"]
