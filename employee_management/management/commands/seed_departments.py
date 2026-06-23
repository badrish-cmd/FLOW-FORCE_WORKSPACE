"""
Management command to seed initial departments

Usage:
    python manage.py seed_departments
"""

from django.core.management.base import BaseCommand
from employee_management.models import Department


class Command(BaseCommand):
    help = 'Create default departments for the system'

    def handle(self, *args, **options):
        departments = [
            {
                'name': 'Operations',
                'description': 'Handles day-to-day operational activities and process management',
                'color': '#3B82F6'
            },
            {
                'name': 'Engineering',
                'description': 'Software development and technical implementation',
                'color': '#8B5CF6'
            },
            {
                'name': 'Sales',
                'description': 'Business development and customer acquisition',
                'color': '#EC4899'
            },
            {
                'name': 'Marketing',
                'description': 'Brand development, campaigns, and customer engagement',
                'color': '#F59E0B'
            },
            {
                'name': 'HR',
                'description': 'Human resources, recruitment, and employee management',
                'color': '#10B981'
            },
            {
                'name': 'Finance',
                'description': 'Accounting, budgeting, and financial management',
                'color': '#06B6D4'
            },
            {
                'name': 'Procurement',
                'description': 'Vendor management and procurement operations',
                'color': '#EF4444'
            },
        ]

        created_count = 0
        for dept_data in departments:
            dept, created = Department.objects.get_or_create(
                name=dept_data['name'],
                defaults={
                    'description': dept_data['description'],
                    'color': dept_data['color'],
                    'is_active': True
                }
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f'✓ Created department: {dept.name}')
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f'→ Department already exists: {dept.name}')
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'\n✅ Successfully created {created_count} new departments'
            )
        )
