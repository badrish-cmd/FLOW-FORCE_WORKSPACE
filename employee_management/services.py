"""
Employee Management Services Layer

Contains all business logic for employee management operations.
Keeps views clean and logic reusable.
"""

from django.db import transaction
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone
from django.contrib.auth.hashers import make_password
import random
import string

from auth_app.models import EmployeeUser
from .models import EmployeeActivityLog, EmployeeLoginHistory, EmployeeApprovalQueue


class EmployeeService:
    """
    Service class for employee management operations.
    Handles creation, updates, deletions, and related operations.
    """
    
    @staticmethod
    def create_employee(
        full_name,
        email,
        department,
        role,
        status,
        password=None,
        created_by=None
    ):
        """
        Create a new employee with audit logging.
        
        Args:
            full_name (str): Employee's full name
            email (str): Employee's email
            department (str): Employee's department
            role (str): Employee's role
            status (str): Employee's status
            password (str): Employee's password (optional, auto-generated if not provided)
            created_by (EmployeeUser): User creating the employee
            
        Returns:
            EmployeeUser: Created employee instance
        """
        
        if password is None:
            password = EmployeeService.generate_password()
        
        with transaction.atomic():
            employee = EmployeeUser.objects.create(
                full_name=full_name,
                email=email.lower().strip(),
                department=department,
                role=role,
                status=status,
                is_staff=(role in ["SUPER_ADMIN", "ADMIN"]),
            )
            employee.set_password(password)
            employee.save()
            
            # Log the activity
            EmployeeActivityLog.objects.create(
                employee=employee,
                activity_type="CREATE",
                performed_by=created_by,
                description=f"Employee created with role: {role}, status: {status}",
                changes={
                    "full_name": full_name,
                    "email": email,
                    "department": department,
                    "role": role,
                    "status": status
                }
            )
            
            # Add to approval queue if status is PENDING
            if status == "PENDING":
                EmployeeApprovalQueue.objects.create(
                    employee=employee,
                    submitted_by=created_by,
                    priority="MEDIUM"
                )
            
        return employee
    
    @staticmethod
    def update_employee(
        employee,
        full_name=None,
        email=None,
        department=None,
        role=None,
        status=None,
        is_active=None,
        updated_by=None
    ):
        """
        Update employee information with change tracking.
        
        Args:
            employee (EmployeeUser): Employee to update
            full_name (str): New full name
            email (str): New email
            department (str): New department
            role (str): New role
            status (str): New status
            is_active (bool): New active status
            updated_by (EmployeeUser): User performing the update
            
        Returns:
            EmployeeUser: Updated employee instance
        """
        
        changes = {}
        original = EmployeeUser.objects.get(pk=employee.pk)
        
        if full_name is not None and full_name != original.full_name:
            changes["full_name"] = {
                "old": original.full_name,
                "new": full_name
            }
            employee.full_name = full_name
        
        if email is not None and email.lower().strip() != original.email:
            email = email.lower().strip()
            if EmployeeUser.objects.filter(email=email).exclude(pk=employee.pk).exists():
                raise ValueError("Email already in use")
            changes["email"] = {
                "old": original.email,
                "new": email
            }
            employee.email = email
        
        if department is not None and department != original.department:
            changes["department"] = {
                "old": original.department,
                "new": department
            }
            employee.department = department
        
        if role is not None and role != original.role:
            changes["role"] = {
                "old": original.role,
                "new": role
            }
            changes["is_staff"] = {
                "old": original.is_staff,
                "new": role in ["SUPER_ADMIN", "ADMIN"]
            }
            employee.role = role
            employee.is_staff = role in ["SUPER_ADMIN", "ADMIN"]
        
        if status is not None and status != original.status:
            changes["status"] = {
                "old": original.status,
                "new": status
            }
            employee.status = status
        
        if is_active is not None and is_active != original.is_active:
            changes["is_active"] = {
                "old": original.is_active,
                "new": is_active
            }
            employee.is_active = is_active
        
        if changes:
            with transaction.atomic():
                employee.save()
                
                # Log the activity
                activity_type = "UPDATE"
                if "role" in changes:
                    activity_type = "ROLE_CHANGE"
                elif "department" in changes:
                    activity_type = "DEPARTMENT_CHANGE"
                elif "status" in changes:
                    activity_type = "STATUS_CHANGE"
                
                EmployeeActivityLog.objects.create(
                    employee=employee,
                    activity_type=activity_type,
                    performed_by=updated_by,
                    description=f"Employee updated",
                    changes=changes
                )
        
        return employee
    
    @staticmethod
    def approve_employee(employee, approved_by, notes=""):
        """
        Approve a pending employee.
        
        Args:
            employee (EmployeeUser): Employee to approve
            approved_by (EmployeeUser): User approving the employee
            notes (str): Approval notes
        """
        
        with transaction.atomic():
            employee.status = "APPROVED"
            employee.save()
            
            # Update approval queue
            try:
                approval = EmployeeApprovalQueue.objects.get(employee=employee)
                approval.is_approved = True
                approval.reviewed_by = approved_by
                approval.reviewed_at = timezone.now()
                approval.approval_notes = notes
                approval.save()
            except EmployeeApprovalQueue.DoesNotExist:
                pass
            
            # Log the activity
            EmployeeActivityLog.objects.create(
                employee=employee,
                activity_type="APPROVE",
                performed_by=approved_by,
                description=f"Employee approved. Notes: {notes}" if notes else "Employee approved",
                changes={"status": {"old": "PENDING", "new": "APPROVED"}}
            )
    
    @staticmethod
    def reject_employee(employee, rejected_by, notes=""):
        """
        Reject a pending employee.
        
        Args:
            employee (EmployeeUser): Employee to reject
            rejected_by (EmployeeUser): User rejecting the employee
            notes (str): Rejection notes
        """
        
        with transaction.atomic():
            employee.status = "REJECTED"
            employee.is_active = False
            employee.save()
            
            # Update approval queue
            try:
                approval = EmployeeApprovalQueue.objects.get(employee=employee)
                approval.is_approved = False
                approval.reviewed_by = rejected_by
                approval.reviewed_at = timezone.now()
                approval.approval_notes = notes
                approval.save()
            except EmployeeApprovalQueue.DoesNotExist:
                pass
            
            # Log the activity
            EmployeeActivityLog.objects.create(
                employee=employee,
                activity_type="REJECT",
                performed_by=rejected_by,
                description=f"Employee rejected. Notes: {notes}" if notes else "Employee rejected",
                changes={"status": {"old": "PENDING", "new": "REJECTED"}}
            )
    
    @staticmethod
    def activate_employee(employee, activated_by):
        """
        Activate a deactivated employee.
        
        Args:
            employee (EmployeeUser): Employee to activate
            activated_by (EmployeeUser): User activating the employee
        """
        
        with transaction.atomic():
            employee.is_active = True
            employee.save()
            
            EmployeeActivityLog.objects.create(
                employee=employee,
                activity_type="ACTIVATE",
                performed_by=activated_by,
                description="Employee account activated",
                changes={"is_active": {"old": False, "new": True}}
            )
    
    @staticmethod
    def deactivate_employee(employee, deactivated_by):
        """
        Deactivate an active employee.
        
        Args:
            employee (EmployeeUser): Employee to deactivate
            deactivated_by (EmployeeUser): User deactivating the employee
        """
        
        with transaction.atomic():
            employee.is_active = False
            employee.save()
            
            EmployeeActivityLog.objects.create(
                employee=employee,
                activity_type="DEACTIVATE",
                performed_by=deactivated_by,
                description="Employee account deactivated",
                changes={"is_active": {"old": True, "new": False}}
            )
    
    @staticmethod
    def reset_password_by_admin(employee, reset_by, new_password=None):
        """
        Reset employee password by admin.
        
        Args:
            employee (EmployeeUser): Employee whose password to reset
            reset_by (EmployeeUser): Admin resetting the password
            new_password (str): New password (auto-generated if not provided)
            
        Returns:
            str: The new password (for sending to employee)
        """
        
        if new_password is None:
            new_password = EmployeeService.generate_password()
        
        with transaction.atomic():
            employee.set_password(new_password)
            employee.save()
            
            EmployeeActivityLog.objects.create(
                employee=employee,
                activity_type="PASSWORD_RESET",
                performed_by=reset_by,
                description="Password reset by administrator"
            )
        
        return new_password
    
    @staticmethod
    def generate_password(length=12):
        """
        Generate a secure random password.
        
        Args:
            length (int): Password length
            
        Returns:
            str: Generated password
        """
        
        characters = string.ascii_letters + string.digits + string.punctuation
        while True:
            password = ''.join(random.choice(characters) for _ in range(length))
            # Ensure password has at least one uppercase, one lowercase, one digit
            if (any(c.isupper() for c in password) and
                any(c.islower() for c in password) and
                any(c.isdigit() for c in password)):
                return password
    
    @staticmethod
    def get_employee_activity_history(employee, limit=50):
        """
        Get activity history for an employee.
        
        Args:
            employee (EmployeeUser): Employee to get history for
            limit (int): Number of records to return
            
        Returns:
            QuerySet: Activity logs
        """
        
        return EmployeeActivityLog.objects.filter(
            employee=employee
        ).order_by("-created_at")[:limit]
    
    @staticmethod
    def get_employee_login_history(employee, limit=50):
        """
        Get login history for an employee.
        
        Args:
            employee (EmployeeUser): Employee to get history for
            limit (int): Number of records to return
            
        Returns:
            QuerySet: Login history records
        """
        
        return EmployeeLoginHistory.objects.filter(
            employee=employee
        ).order_by("-login_at")[:limit]
    
    @staticmethod
    def log_login(employee, ip_address="", user_agent="", session_key=""):
        """
        Log an employee login.
        
        Args:
            employee (EmployeeUser): Employee logging in
            ip_address (str): User's IP address
            user_agent (str): User's browser user agent
            session_key (str): Django session key
        """
        
        EmployeeLoginHistory.objects.create(
            employee=employee,
            ip_address=ip_address,
            user_agent=user_agent,
            session_key=session_key
        )
        
        EmployeeActivityLog.objects.create(
            employee=employee,
            activity_type="LOGIN",
            performed_by=employee,
            description="Employee logged in",
            ip_address=ip_address,
            user_agent=user_agent
        )
    
    @staticmethod
    def log_logout(employee):
        """
        Log an employee logout.
        
        Args:
            employee (EmployeeUser): Employee logging out
        """
        
        # Update the latest login session to mark logout time
        try:
            latest_login = EmployeeLoginHistory.objects.filter(
                employee=employee,
                is_active=True
            ).latest("login_at")
            latest_login.logout_at = timezone.now()
            latest_login.is_active = False
            latest_login.save()
        except EmployeeLoginHistory.DoesNotExist:
            pass
        
        EmployeeActivityLog.objects.create(
            employee=employee,
            activity_type="LOGOUT",
            performed_by=employee,
            description="Employee logged out"
        )
    
    @staticmethod
    def get_pending_approvals():
        """
        Get all pending employee approvals.
        
        Returns:
            QuerySet: Pending approval queue items
        """
        
        return EmployeeApprovalQueue.objects.filter(
            is_approved=False
        ).order_by("-submitted_at")


class EmployeeExportService:
    """
    Service for exporting employee data.
    """
    
    @staticmethod
    def export_to_dict(employee):
        """
        Export employee data to dictionary.
        
        Args:
            employee (EmployeeUser): Employee to export
            
        Returns:
            dict: Employee data
        """
        
        return {
            "id": employee.id,
            "full_name": employee.full_name,
            "email": employee.email,
            "department": employee.department,
            "role": employee.role,
            "status": employee.status,
            "is_active": employee.is_active,
            "created_at": employee.created_at.isoformat(),
            "updated_at": employee.updated_at.isoformat(),
        }
    
    @staticmethod
    def export_employees_to_csv(employees):
        """
        Generate CSV data for employees.
        
        Args:
            employees (QuerySet): Employees to export
            
        Returns:
            str: CSV formatted data
        """
        
        import csv
        from io import StringIO
        
        output = StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "ID",
                "Full Name",
                "Email",
                "Department",
                "Role",
                "Status",
                "Active",
                "Created Date",
                "Updated Date"
            ]
        )
        writer.writeheader()
        
        for employee in employees:
            writer.writerow({
                "ID": employee.id,
                "Full Name": employee.full_name,
                "Email": employee.email,
                "Department": employee.department,
                "Role": employee.get_role_display(),
                "Status": employee.get_status_display(),
                "Active": "Yes" if employee.is_active else "No",
                "Created Date": employee.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "Updated Date": employee.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            })
        
        return output.getvalue()
