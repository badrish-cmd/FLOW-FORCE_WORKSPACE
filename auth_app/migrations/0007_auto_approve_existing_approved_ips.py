from django.db import migrations
from django.utils import timezone


def auto_approve_approved_ips(apps, schema_editor):
    AuthorizedDevice = apps.get_model('auth_app', 'AuthorizedDevice')
    # Find all IPs that have at least one APPROVED device
    approved_ips = set(
        AuthorizedDevice.objects.filter(status='APPROVED')
        .exclude(registration_ip__isnull=True)
        .exclude(registration_ip='')
        .values_list('registration_ip', flat=True)
    )
    if approved_ips:
        AuthorizedDevice.objects.filter(
            status='PENDING',
            registration_ip__in=approved_ips
        ).update(
            status='APPROVED',
            approved_at=timezone.now()
        )


class Migration(migrations.Migration):

    dependencies = [
        ('auth_app', '0006_authorizeddevice'),
    ]

    operations = [
        migrations.RunPython(auto_approve_approved_ips, reverse_code=migrations.RunPython.noop),
    ]
