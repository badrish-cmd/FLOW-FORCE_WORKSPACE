from django.db import models
from django.conf import settings
from employee_management.models import Department

class Table(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_new_tables"
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="new_tables"
    )
    is_active = models.BooleanField(default=True)
    job_type = models.CharField(
        max_length=50,
        choices=[("SALES", "Sales"), ("GENERAL", "General"), ("ENGINEER", "Engineer")],
        default="GENERAL"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            # Create system columns: S_NO, DATE, DUE_DATE, TASK_NAME, INITIAL_MAIL, ALERT_MAIL
            system_cols = [
                ("S_NO", "NUMBER", 1),
                ("DATE", "DATE", 2),
                ("DUE_DATE", "DATE", 3),
                ("TASK_NAME", "TEXT", 4),
                ("INITIAL_MAIL", "TEXT", 5),
                ("ALERT_MAIL", "TEXT", 6),
            ]
            for name, dtype, pos in system_cols:
                Column.objects.create(
                    table=self,
                    name=name,
                    data_type=dtype,
                    is_mandatory=True,
                    is_system_column=True,
                    position=pos
                )
            if self.job_type == "ENGINEER":
                Column.objects.create(
                    table=self,
                    name="PID",
                    data_type="TEXT",
                    is_mandatory=False,
                    is_system_column=True,
                    position=7
                )

class Column(models.Model):
    DATA_TYPE_CHOICES = [
        ("TEXT", "Text"),
        ("NUMBER", "Number"),
        ("DATE", "Date"),
        ("DATETIME", "Datetime"),
        ("CHECKBOX", "Checkbox"),
        ("DROPDOWN", "Dropdown"),
        ("USER", "Employee/User"),
    ]

    table = models.ForeignKey(
        Table,
        on_delete=models.CASCADE,
        related_name="columns",
        db_column="table_fk"
    )
    name = models.CharField(max_length=255)
    data_type = models.CharField(
        max_length=50,
        choices=DATA_TYPE_CHOICES,
        default="TEXT"
    )
    is_mandatory = models.BooleanField(default=False)
    is_system_column = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)
    options = models.TextField(blank=True, null=True, help_text="Comma-separated options for dropdown")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.table.name} - {self.name}"

class Row(models.Model):
    table = models.ForeignKey(
        Table,
        on_delete=models.CASCADE,
        related_name="rows",
        db_column="table_fk"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_new_rows"
    )
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"Row {self.id} in {self.table.name}"

class CellValue(models.Model):
    row = models.ForeignKey(
        Row,
        on_delete=models.CASCADE,
        related_name="cells",
        db_column="row_fk"
    )
    column = models.ForeignKey(
        Column,
        on_delete=models.CASCADE,
        related_name="cells",
        db_column="column_fk"
    )
    value = models.JSONField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="updated_cells"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("row", "column")]

    def __str__(self):
        return f"Cell {self.row_id}:{self.column_id} = {self.value}"

class TableAccess(models.Model):
    ACCESS_LEVEL_CHOICES = [
        ("VIEW", "View Only"),
        ("EDIT", "Edit cells"),
        ("ADMIN", "Full Admin"),
    ]

    table = models.ForeignKey(
        Table,
        on_delete=models.CASCADE,
        related_name="access_rules",
        db_column="table_fk"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="table_accesses"
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="table_accesses"
    )
    access_level = models.CharField(
        max_length=50,
        choices=ACCESS_LEVEL_CHOICES,
        default="VIEW"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("table", "user"), ("table", "department")]

    def __str__(self):
        target = f"User {self.user.email}" if self.user else f"Dept {self.department.name}"
        return f"{self.table.name} access for {target} -> {self.access_level}"

class ColumnAccess(models.Model):
    ACCESS_LEVEL_CHOICES = [
        ("HIDDEN", "Hidden"),
        ("READ_ONLY", "Read Only"),
        ("EDITABLE", "Editable"),
    ]

    column = models.ForeignKey(
        Column,
        on_delete=models.CASCADE,
        related_name="access_rules",
        db_column="column_fk"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="column_accesses"
    )
    access_level = models.CharField(
        max_length=50,
        choices=ACCESS_LEVEL_CHOICES,
        default="EDITABLE"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("column", "user")]

    def __str__(self):
        return f"{self.column.name} access for {self.user.email} -> {self.access_level}"
