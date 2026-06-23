# Task Tracker Scheduling

## Daily reminders
Run once every day at 08:00 local time:

```powershell
cd D:\flow-force_workspace
.\venv\Scripts\python.exe manage.py send_due_task_alerts
```

## Overdue escalations
Run once every day after the reminder job:

```powershell
cd D:\flow-force_workspace
.\venv\Scripts\python.exe manage.py send_overdue_escalations
```

## Windows Task Scheduler
Create two scheduled tasks:

- `Flow-Force Due Alerts` at `08:00`
- `Flow-Force Overdue Escalations` at `08:05`

Use the same command lines above and point the task working directory at the workspace root.

## Cron example

```cron
0 8 * * * /path/to/venv/bin/python /path/to/flow-force_workspace/manage.py send_due_task_alerts
5 8 * * * /path/to/venv/bin/python /path/to/flow-force_workspace/manage.py send_overdue_escalations
```

## Celery Beat example

If you prefer Celery scheduling, add recurring entries in `celery.py` or your beat schedule configuration:

```python
from datetime import timedelta

CELERY_BEAT_SCHEDULE = {
    'send-due-task-alerts': {
        'task': 'task_tracker.send_due_task_alerts',
        'schedule': timedelta(hours=24),
        'options': {'expires': 3600},
    },
    'send-overdue-escalations': {
        'task': 'task_tracker.send_overdue_escalations',
        'schedule': timedelta(hours=24, minutes=5),
        'options': {'expires': 3600},
    },
}
```

Then create lightweight Celery tasks that call the management commands or service functions.

## Running Celery

From the project root, start a worker and beat scheduler:

```powershell
cd D:\flow-force_workspace
.\venv\Scripts\celery -A flowforce worker --loglevel=info
.\venv\Scripts\celery -A flowforce beat --loglevel=info
```

For a single process with both worker and beat:

```powershell
.\venv\Scripts\celery -A flowforce worker --beat --loglevel=info
```

## Email configuration

For production email delivery, ensure the project environment provides valid SMTP settings in `flowforce/settings.py` or environment variables:

- `EMAIL_BACKEND`
- `EMAIL_HOST`
- `EMAIL_PORT`
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `DEFAULT_FROM_EMAIL`

For local development, use Django's console or local file backend instead of SMTP:

```python
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
```
