from django.test import TestCase
from django.urls import reverse
from auth_app.models import EmployeeUser
from employee_management.models import Department
from employee_management.forms import EmployeeForm

class EmployeeManagementTests(TestCase):
    def setUp(self):
        # Get or create departments to avoid unique constraint violations if populated by migrations
        self.dept_sales, _ = Department.objects.get_or_create(name="Sales")
        self.dept_eng, _ = Department.objects.get_or_create(name="Engineering")

        # Create admin user
        self.admin = EmployeeUser.objects.create_user(
            email="admin.user@flow-force.com",
            password="testpassword123",
            full_name="Admin User",
            role="ADMIN",
            status="APPROVED",
            department=self.dept_sales
        )

        # Create super admin user
        self.super_admin = EmployeeUser.objects.create_user(
            email="super.user@flow-force.com",
            password="testpassword123",
            full_name="Super User",
            role="SUPER_ADMIN",
            status="APPROVED"
        )

        # Create employee user
        self.employee = EmployeeUser.objects.create_user(
            email="emp.user@flow-force.com",
            password="testpassword123",
            full_name="Employee User",
            role="EMPLOYEE",
            status="APPROVED",
            department=self.dept_sales
        )

        # Create department admin user
        self.dept_admin = EmployeeUser.objects.create_user(
            email="dept.admin@flow-force.com",
            password="testpassword123",
            full_name="Dept Admin User",
            role="DEPARTMENT_ADMIN",
            status="APPROVED",
            department=self.dept_sales
        )

    def test_admin_can_promote_employee_to_admin(self):
        self.client.login(username=self.admin.email, password="testpassword123")
        
        # Test editing employee and changing role to ADMIN
        url = reverse("employee_edit", kwargs={"employee_id": self.employee.id})
        response = self.client.post(url, {
            "full_name": self.employee.full_name,
            "email": self.employee.email,
            "department": self.dept_sales.id,
            "role": "ADMIN",
            "status": "APPROVED",
            "is_active": True
        })
        self.assertEqual(response.status_code, 302)
        
        # Verify the role was changed to ADMIN
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.role, "ADMIN")
        self.assertTrue(self.employee.is_staff)

    def test_admin_can_edit_another_admin(self):
        # Create another admin
        other_admin = EmployeeUser.objects.create_user(
            email="other.admin@flow-force.com",
            password="testpassword123",
            full_name="Other Admin",
            role="ADMIN",
            status="APPROVED"
        )
        
        self.client.login(username=self.admin.email, password="testpassword123")
        
        url = reverse("employee_edit", kwargs={"employee_id": other_admin.id})
        response = self.client.post(url, {
            "full_name": "Updated Other Admin Name",
            "email": other_admin.email,
            "department": "",
            "role": "EMPLOYEE",  # Downgrade to employee
            "status": "APPROVED",
            "is_active": True
        })
        self.assertEqual(response.status_code, 302)
        
        other_admin.refresh_from_db()
        self.assertEqual(other_admin.full_name, "Updated Other Admin Name")
        self.assertEqual(other_admin.role, "EMPLOYEE")

    def test_admin_cannot_promote_to_super_admin(self):
        self.client.login(username=self.admin.email, password="testpassword123")
        
        url = reverse("employee_edit", kwargs={"employee_id": self.employee.id})
        response = self.client.post(url, {
            "full_name": self.employee.full_name,
            "email": self.employee.email,
            "department": self.dept_sales.id,
            "role": "SUPER_ADMIN",
            "status": "APPROVED",
            "is_active": True
        })
        # Should fail validation
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, "form", "role", "Select a valid choice. SUPER_ADMIN is not one of the available choices.")

    def test_department_admin_cannot_promote_to_admin(self):
        self.client.login(username=self.dept_admin.email, password="testpassword123")
        
        url = reverse("employee_edit", kwargs={"employee_id": self.employee.id})
        response = self.client.post(url, {
            "full_name": self.employee.full_name,
            "email": self.employee.email,
            "department": self.dept_sales.id,
            "role": "ADMIN",
            "status": "APPROVED",
            "is_active": True
        })
        # Should fail validation
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, "form", "role", "Select a valid choice. ADMIN is not one of the available choices.")
