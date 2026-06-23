from django.db import models
from django.utils import timezone
from auth_app.models import EmployeeUser
from django.core.validators import FileExtensionValidator


class Department(models.Model):
    """
    Dynamic department model.
    Allows admins to create and manage departments without code changes.
    Replaces hardcoded department choices.
    """
    
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Department name (e.g., Engineering, Sales, HR)"
    )
    
    slug = models.SlugField(
        unique=True,
        editable=False,
        help_text="URL-friendly identifier"
    )
    
    description = models.TextField(
        blank=True,
        help_text="Department description and responsibilities"
    )
    
    head = models.ForeignKey(
        EmployeeUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="departments_headed",
        help_text="Department head/manager"
    )
    
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive departments are hidden from selection"
    )
    
    color = models.CharField(
        max_length=7,
        default="#1F2937",
        help_text="Hex color for UI display (e.g., #FF5733)"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Departments"
        indexes = [
            models.Index(fields=["is_active", "name"]),
            models.Index(fields=["slug"]),
        ]
    
    def __str__(self):
        return self.name
    
    def save(self, *args, **kwargs):
        """Auto-generate slug from name"""
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)
    
    @property
    def employee_count(self):
        """Get count of employees in this department"""
        from auth_app.models import EmployeeUser
        return EmployeeUser.objects.filter(
            department=self,
            is_active=True
        ).count()


class ManagedEmployee(EmployeeUser):
    """
    Proxy model for EmployeeUser to manage employees through the app.
    Provides a clean interface for employee management operations.
    """
    class Meta:
        proxy = True
        verbose_name = "Managed Employee"
        verbose_name_plural = "Managed Employees"


class EmployeeActivityLog(models.Model):
    """
    Audit trail for all employee-related activities.
    Tracks creation, updates, deletions, status changes, role assignments, etc.
    """
    
    ACTIVITY_TYPES = [
        ("CREATE", "Employee Created"),
        ("UPDATE", "Employee Updated"),
        ("DELETE", "Employee Deleted"),
        ("ACTIVATE", "Account Activated"),
        ("DEACTIVATE", "Account Deactivated"),
        ("APPROVE", "Employee Approved"),
        ("REJECT", "Employee Rejected"),
        ("ROLE_CHANGE", "Role Changed"),
        ("DEPARTMENT_CHANGE", "Department Changed"),
        ("PASSWORD_RESET", "Password Reset"),
        ("STATUS_CHANGE", "Status Changed"),
        ("LOGIN", "Login"),
        ("LOGOUT", "Logout"),
        ("EXPORT", "Data Exported"),
        ("BULK_ACTION", "Bulk Action"),
    ]
    
    employee = models.ForeignKey(
        EmployeeUser,
        on_delete=models.CASCADE,
        related_name="activity_logs"
    )
    
    activity_type = models.CharField(
        max_length=50,
        choices=ACTIVITY_TYPES
    )
    
    performed_by = models.ForeignKey(
        EmployeeUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="performed_activities"
    )
    
    description = models.TextField(blank=True)
    
    changes = models.JSONField(
        default=dict,
        blank=True,
        help_text="JSON field storing before/after values for updates"
    )
    
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True
    )
    
    user_agent = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["employee", "-created_at"]),
            models.Index(fields=["activity_type", "-created_at"]),
            models.Index(fields=["performed_by", "-created_at"]),
        ]
    
    def __str__(self):
        return f"{self.employee.email} - {self.get_activity_type_display()} - {self.created_at}"


class EmployeeLoginHistory(models.Model):
    """
    Track login timestamps and sessions for each employee.
    Helps with security monitoring and activity tracking.
    """
    
    employee = models.ForeignKey(
        EmployeeUser,
        on_delete=models.CASCADE,
        related_name="login_history"
    )
    
    login_at = models.DateTimeField(auto_now_add=True)
    
    logout_at = models.DateTimeField(
        null=True,
        blank=True
    )
    
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True
    )
    
    user_agent = models.TextField(blank=True)
    
    session_key = models.CharField(
        max_length=40,
        blank=True
    )
    
    is_active = models.BooleanField(
        default=True
    )
    
    class Meta:
        ordering = ["-login_at"]
        indexes = [
            models.Index(fields=["employee", "-login_at"]),
            models.Index(fields=["is_active"]),
        ]
    
    def __str__(self):
        return f"{self.employee.email} - {self.login_at}"
    
    @property
    def session_duration(self):
        """Calculate session duration in minutes"""
        if self.logout_at:
            return int((self.logout_at - self.login_at).total_seconds() / 60)
        return None


class EmployeeProfilePicture(models.Model):
    """
    Store profile pictures for employees.
    Supports version history and multiple images.
    """
    
    employee = models.OneToOneField(
        EmployeeUser,
        on_delete=models.CASCADE,
        related_name="profile_picture"
    )
    
    image = models.ImageField(
        upload_to="profile_pictures/%Y/%m/%d/",
        validators=[FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "gif", "webp"])]
    )
    
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    is_current = models.BooleanField(default=True)
    
    class Meta:
        ordering = ["-uploaded_at"]
    
    def __str__(self):
        return f"{self.employee.email} - Profile Picture"


class EmployeeApprovalQueue(models.Model):
    """
    Track employees pending approval.
    Manages approval workflow state.
    """
    
    PRIORITY_CHOICES = [
        ("LOW", "Low"),
        ("MEDIUM", "Medium"),
        ("HIGH", "High"),
        ("URGENT", "Urgent"),
    ]
    
    employee = models.OneToOneField(
        EmployeeUser,
        on_delete=models.CASCADE,
        related_name="approval_queue"
    )
    
    submitted_by = models.ForeignKey(
        EmployeeUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="submitted_approvals"
    )
    
    priority = models.CharField(
        max_length=20,
        choices=PRIORITY_CHOICES,
        default="MEDIUM"
    )
    
    notes = models.TextField(blank=True)
    
    submitted_at = models.DateTimeField(auto_now_add=True)
    
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True
    )
    
    reviewed_by = models.ForeignKey(
        EmployeeUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_approvals"
    )
    
    approval_notes = models.TextField(blank=True)
    
    is_approved = models.BooleanField(default=False)
    
    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["-submitted_at"]),
            models.Index(fields=["is_approved"]),
        ]
    
    def __str__(self):
        status = "Approved" if self.is_approved else "Pending"
        return f"{self.employee.email} - {status}"


class Team(models.Model):
    """
    A grouping of employees for collaborative tracking and sharing.
    Admins can share tables/trackers with Teams.
    """
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    members = models.ManyToManyField(EmployeeUser, related_name="teams", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
