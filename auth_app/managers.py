from django.contrib.auth.base_user import BaseUserManager


class EmployeeUserManager(BaseUserManager):

    def create_user(
        self,
        email,
        password=None,
        **extra_fields
    ):
        if not email:
            raise ValueError(
                "Email is required"
            )

        email = self.normalize_email(email)

        # Resolve department string or key to a Department instance
        dept = extra_fields.get("department")
        if dept and isinstance(dept, str):
            from employee_management.models import Department
            dept_obj = Department.objects.filter(name__iexact=dept.strip()).first()
            if not dept_obj:
                dept_obj = Department.objects.create(name=dept.strip())
            extra_fields["department"] = dept_obj

        user = self.model(
            email=email,
            **extra_fields
        )

        user.set_password(password)

        user.save(using=self._db)

        return user

    def create_superuser(
        self,
        email,
        password,
        **extra_fields
    ):

        extra_fields.setdefault(
            "role",
            "SUPER_ADMIN"
        )

        extra_fields.setdefault(
            "is_staff",
            True
        )

        extra_fields.setdefault(
            "is_superuser",
            True
        )

        extra_fields.setdefault(
            "is_active",
            True
        )

        if extra_fields.get("is_staff") is not True:
            raise ValueError(
                "Superuser must have is_staff=True."
            )

        if extra_fields.get("is_superuser") is not True:
            raise ValueError(
                "Superuser must have is_superuser=True."
            )

        return self.create_user(
            email,
            password,
            **extra_fields
        )