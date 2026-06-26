from rest_framework import serializers
from .models import Task, TaskComment, ActivityLog, Notification, EmailLog, TaskFollowUp
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

class TaskFollowUpSerializer(serializers.ModelSerializer):
    entered_by_detail = UserMinSerializer(source="entered_by", read_only=True)

    class Meta:
        model = TaskFollowUp
        fields = ["id", "task", "follow_up_date", "discussed_points", "next_follow_up_date", "entered_by", "entered_by_detail", "created_at"]

class TaskSerializer(serializers.ModelSerializer):
    assigned_to_details = UserMinSerializer(source="assigned_to", many=True, read_only=True)
    assigned_by_detail = UserMinSerializer(source="assigned_by", read_only=True)
    comments = TaskCommentSerializer(many=True, read_only=True)
    activity_logs = ActivityLogSerializer(many=True, read_only=True)
    follow_ups = TaskFollowUpSerializer(many=True, read_only=True)

    class Meta:
        model = Task
        fields = [
            "id", "row", "assigned_by", "assigned_by_detail", "assigned_to", "assigned_to_details",
            "status", "due_date", "priority", "created_at", "updated_at", "comments", "activity_logs", "follow_ups"
        ]
