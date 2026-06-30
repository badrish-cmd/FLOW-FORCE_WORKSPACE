from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.template.loader import render_to_string
from django.db import transaction
from .models import Task, EmailLog, Notification, ActivityLog
from tables.models import CellValue, Column
from auth_app.models import EmployeeUser

def update_task_row_mail_columns(task):
    """
    Update row cell values for INITIAL_MAIL and ALERT_MAIL columns.
    """
    try:
        row = getattr(task, 'row', None)
        if not row:
            return
        
        # For INITIAL_MAIL
        init_val = "YES" if getattr(task, 'initial_mail_sent', False) else "NO"
        init_col = Column.objects.filter(table=row.table, name__iexact="INITIAL_MAIL").first()
        if not init_col:
            init_col = Column.objects.filter(table=row.table, name__icontains="INITIAL").filter(name__icontains="MAIL").first()
        if not init_col:
            init_col = Column.objects.filter(table=row.table, name__icontains="INITIAL").first()
            
        if init_col:
            CellValue.objects.update_or_create(
                row=row,
                column=init_col,
                defaults={"value": init_val}
            )
    except Exception:
        pass
        
    try:
        row = getattr(task, 'row', None)
        if not row:
            return
        # For ALERT_MAIL
        alert_val = "YES" if getattr(task, 'alert_mail_sent', False) else "NO"
        alert_col = Column.objects.filter(table=row.table, name__iexact="ALERT_MAIL").first()
        if not alert_col:
            alert_col = Column.objects.filter(table=row.table, name__icontains="ALERT").filter(name__icontains="MAIL").first()
        if not alert_col:
            alert_col = Column.objects.filter(table=row.table, name__icontains="ALERT").first()
            
        if alert_col:
            CellValue.objects.update_or_create(
                row=row,
                column=alert_col,
                defaults={"value": alert_val}
            )
    except Exception:
        pass

def notify_admin_of_failure(email_log):
    """
    Notify all Super Admins when email retries are exhausted.
    """
    super_admins = EmployeeUser.objects.filter(role="SUPER_ADMIN")
    for admin in super_admins:
        Notification.objects.create(
            user=admin,
            task=email_log.task,
            title="Email Delivery Failed",
            description=f"Failed to send {email_log.email_type} to {email_log.recipient_email} after {email_log.retry_count} retries.",
            type="SYSTEM"
        )

@shared_task(bind=True)
def send_email_log_task(self, email_log_id):
    """
    Process a single EmailLog, retry on failure with exponential backoff.
    """
    try:
        email_log = EmailLog.objects.get(pk=email_log_id)
    except EmailLog.DoesNotExist:
        return

    if email_log.status == "SENT":
        return

    try:
        # Send mail
        from_email = 'operations.flowforce@gmail.com' if email_log.email_type in ["INITIAL_MAIL", "ALERT_MAIL", "OVERDUE_ESCALATION_MAIL"] else settings.DEFAULT_FROM_EMAIL
        send_mail(
            subject=email_log.subject,
            message=email_log.body or "",  # plain text fallback
            from_email=from_email,
            recipient_list=[email_log.recipient_email],
            html_message=email_log.body if email_log.body and "<" in email_log.body else None,
            fail_silently=False,
        )
        
        email_log.status = "SENT"
        email_log.sent_at = timezone.now()
        email_log.error_message = None
        email_log.save()

        # Post-send triggers
        task = email_log.task
        if task:
            if email_log.email_type == "INITIAL_MAIL":
                task.initial_mail_sent = True
                task.save()
                update_task_row_mail_columns(task)
                
                # Create Notification entry
                for employee in task.assigned_to.all():
                    if employee.email == email_log.recipient_email:
                        Notification.objects.create(
                            user=employee,
                            task=task,
                            title="Task Assigned to You",
                            description=f"You have been assigned a task by {task.assigned_by.full_name if task.assigned_by else 'System'}",
                            type="ASSIGNED"
                        )
                        ActivityLog.objects.create(
                            task=task,
                            action="INITIAL_MAIL Sent",
                            details={"recipient": employee.email}
                        )
            elif email_log.email_type == "ALERT_MAIL":
                task.alert_mail_sent = True
                task.save()
                update_task_row_mail_columns(task)
                
                for employee in task.assigned_to.all():
                    if employee.email == email_log.recipient_email:
                        Notification.objects.create(
                            user=employee,
                            task=task,
                            title="Task Due Today",
                            description=f"Your task '{task.task_name}' is due today.",
                            type="DUE_TODAY"
                        )
            elif email_log.email_type == "REVIEW_REQUEST_MAIL":
                if task.assigned_by:
                    Notification.objects.create(
                        user=task.assigned_by,
                        task=task,
                        title="Task Ready for Review",
                        description=f"Task '{task.task_name}' is ready for review.",
                        type="REVIEW"
                    )
                    ActivityLog.objects.create(
                        task=task,
                        action="REVIEW_REQUEST_MAIL Sent",
                        details={"recipient": task.assigned_by.email}
                    )
            elif email_log.email_type == "APPROVAL_STATUS_MAIL":
                for employee in task.assigned_to.all():
                    if employee.email == email_log.recipient_email:
                        Notification.objects.create(
                            user=employee,
                            task=task,
                            title=f"Task Status Updated: {task.status}",
                            description=f"Your task status was updated to {task.status}.",
                            type="SYSTEM"
                        )
                        ActivityLog.objects.create(
                            task=task,
                            action="APPROVAL_STATUS_MAIL Sent",
                            details={"recipient": employee.email, "status": task.status}
                        )
            elif email_log.email_type == "OVERDUE_ESCALATION_MAIL":
                recipient_user = EmployeeUser.objects.filter(email=email_log.recipient_email).first()
                if recipient_user:
                    days_overdue = (timezone.localdate() - task.due_date).days
                    Notification.objects.create(
                        user=recipient_user,
                        task=task,
                        title=f"ESCALATION: Overdue Task ({days_overdue} days)",
                        description=f"Task '{task.task_name}' is {days_overdue} days overdue.",
                        type="SYSTEM"
                    )

    except Exception as e:
        email_log.status = "FAILED"
        email_log.error_message = str(e)
        
        if email_log.retry_count < email_log.max_retries:
            if email_log.email_type == "INITIAL_MAIL":
                backoffs = [60, 300, 900]
            elif email_log.email_type == "ALERT_MAIL":
                backoffs = [60, 300]
            else:
                backoffs = [60, 300, 900]
            
            idx = min(email_log.retry_count, len(backoffs) - 1)
            delay_seconds = backoffs[idx]
            
            email_log.next_retry_at = timezone.now() + timezone.timedelta(seconds=delay_seconds)
            email_log.retry_count += 1
            email_log.save()
            
            send_email_log_task.apply_async(args=[email_log_id], countdown=delay_seconds)
        else:
            email_log.save()
            notify_admin_of_failure(email_log)

@shared_task
def send_initial_mail(task_id):
    """
    Trigger INITIAL_MAIL for assigned employees.
    """
    try:
        task = Task.objects.get(id=task_id)
    except Task.DoesNotExist:
        return

    # Check if assigned by an admin
    if not task.assigned_by or task.assigned_by.role not in ["ADMIN", "SUPER_ADMIN", "DEPARTMENT_ADMIN"]:
        return

    # Do not send if task is completed or approved
    if task.status in ["COMPLETED", "APPROVED"]:
        return

    # Do not send if initial mail was already sent
    if task.initial_mail_sent:
        return

    init_mail_col = Column.objects.filter(table=task.row.table, name__iexact="INITIAL_MAIL").first()
    if init_mail_col:
        init_cell = CellValue.objects.filter(row=task.row, column=init_mail_col).first()
        if init_cell and str(init_cell.value).upper() == "YES":
            return

    for employee in task.assigned_to.all():
        if task.assigned_by == employee:
            continue

        # Do not send email if assignee is admin or super admin
        if employee.role in ["ADMIN", "SUPER_ADMIN"]:
            continue

        if EmailLog.objects.filter(task=task, recipient_email=employee.email, email_type="INITIAL_MAIL").exists():
            continue

        subject = f"Task: {task.task_name} - Due Date: {task.due_date}"
        body = f"Task Name: {task.task_name}\nDue Date: {task.due_date}"

        email_log = EmailLog.objects.create(
            recipient_email=employee.email,
            subject=subject,
            body=body,
            task=task,
            email_type='INITIAL_MAIL',
            status='PENDING',
            max_retries=3,
        )

        send_email_log_task.delay(email_log.id)

@shared_task
def send_daily_alert_mails():
    """
    Find active tasks due today, group by assignee, and send a single consolidated
    email and in-app notification summary to each employee.
    """
    today = timezone.localdate()
    tasks = Task.objects.filter(
        due_date=today
    ).exclude(
        status__in=['COMPLETED', 'APPROVED']
    ).select_related('row', 'row__table', 'assigned_by').prefetch_related('assigned_to')

    # Group tasks by assigned employee
    employee_tasks = {}
    for task in tasks:
        for employee in task.assigned_to.all():
            # Skip alerts for admin users by default
            if employee.role in ["ADMIN", "SUPER_ADMIN"]:
                continue
            if employee not in employee_tasks:
                employee_tasks[employee] = []
            employee_tasks[employee].append(task)

    for employee, tasks_list in employee_tasks.items():
        tasks_to_alert = []
        for task in tasks_list:
            # Check if alert_mail_sent is already True
            if task.alert_mail_sent:
                continue

            # Double check with spreadsheet CellValue directly
            alert_mail_col = Column.objects.filter(table=task.row.table, name__iexact="ALERT_MAIL").first()
            if alert_mail_col:
                alert_cell = CellValue.objects.filter(row=task.row, column=alert_mail_col).first()
                if alert_cell and str(alert_cell.value).upper() == "YES":
                    # Keep DB status in sync
                    task.alert_mail_sent = True
                    task.save(update_fields=['alert_mail_sent'])
                    continue

            tasks_to_alert.append(task)

        if not tasks_to_alert:
            continue

        # 1. Create in-app notifications separated by task type
        sales_tasks = [t for t in tasks_to_alert if t.row.table.job_type in ["SALES", "LIST_PID"]]
        other_tasks = [t for t in tasks_to_alert if t.row.table.job_type not in ["SALES", "LIST_PID"]]

        if sales_tasks:
            Notification.objects.create(
                user=employee,
                title=f"Sales Follow-ups Due Today",
                description=f"You have {len(sales_tasks)} sales follow-up task(s) due today. Kindly enter new follow up.",
                type="DUE_TODAY"
            )

        if other_tasks:
            Notification.objects.create(
                user=employee,
                title=f"Tasks Due Today",
                description=f"You have {len(other_tasks)} tasks due today. Kindly check on them and update.",
                type="DUE_TODAY"
            )

        # 2. Build consolidated email log details
        task_items = []
        for task in tasks_to_alert:
            is_sales = task.row.table.job_type in ["SALES", "LIST_PID"]
            site_url = getattr(settings, 'SITE_URL', 'https://flowforceworkspace.cloud')
            task_link = f"{site_url}/tables/{task.row.table_id}/?open_task_id={task.id}"
            
            # Fetch last follow-up discussion points for sales tasks
            last_discussion = None
            if is_sales:
                last_follow_up = task.follow_ups.first()
                last_discussion = last_follow_up.discussed_points if last_follow_up else "No previous follow-ups logged."

            task_items.append({
                'name': task.task_name,
                'table_name': task.row.table.name,
                'priority': task.priority,
                'link': task_link,
                'last_discussion': last_discussion
            })

            # Update the task status to alert_mail_sent = True in DB
            task.alert_mail_sent = True
            task.save(update_fields=['alert_mail_sent'])
            
            # Sync back to the row cell value
            alert_mail_col = Column.objects.filter(table=task.row.table, name__iexact="ALERT_MAIL").first()
            if alert_mail_col:
                CellValue.objects.update_or_create(
                    row=task.row,
                    column=alert_mail_col,
                    defaults={"value": "YES"}
                )

        # Build dynamic intro text for consolidated email
        if sales_tasks and not other_tasks:
            email_intro = f"You have <strong>{len(sales_tasks)}</strong> sales task(s) due today. Kindly log discussions and enter new follow up in the workspace."
        elif other_tasks and not sales_tasks:
            email_intro = f"You have <strong>{len(other_tasks)}</strong> tasks due today. Kindly check on them and update."
        else:
            email_intro = f"You have <strong>{len(tasks_to_alert)}</strong> task(s) due today (<strong>{len(sales_tasks)}</strong> sales and <strong>{len(other_tasks)}</strong> other). Kindly check them and update in the workspace."

        subject = f"Daily Alert Summary: {len(tasks_to_alert)} Task(s) Due Today"
        context = {
            'employee_name': employee.full_name or employee.email,
            'task_count': len(tasks_to_alert),
            'task_items': task_items,
            'email_intro': email_intro
        }
        html_message = render_to_string('emails/consolidated_alert_mail.html', context)

        # Create consolidated EmailLog
        email_log = EmailLog.objects.create(
            recipient_email=employee.email,
            subject=subject,
            body=html_message,
            task=tasks_to_alert[0], # Link to first task in log
            email_type='ALERT_MAIL',
            status='PENDING',
            max_retries=2,
        )

        # Trigger Celery task asynchronously
        send_email_log_task.delay(email_log.id)

@shared_task
def send_alert_mail(task_id):
    """
    Trigger ALERT_MAIL for assigned employees.
    """
    try:
        task = Task.objects.get(id=task_id)
    except Task.DoesNotExist:
        return

    # Do not send if task is completed or approved
    if task.status in ["COMPLETED", "APPROVED"]:
        return

    # Do not send if alert mail was already sent
    if task.alert_mail_sent:
        return

    alert_mail_col = Column.objects.filter(table=task.row.table, name__iexact="ALERT_MAIL").first()
    if alert_mail_col:
        alert_cell = CellValue.objects.filter(row=task.row, column=alert_mail_col).first()
        if alert_cell and str(alert_cell.value).upper() == "YES":
            return

    for employee in task.assigned_to.all():
        # Do not send email if assignee is admin or super admin
        if employee.role in ["ADMIN", "SUPER_ADMIN"]:
            continue

        is_sales = task.row.table.job_type in ["SALES", "LIST_PID"]
        
        # If it is not a sales task, check if ALERT_MAIL has already been logged.
        # For sales, rolling follow-ups require emails to be sent on every new follow-up date,
        # so we check if ALERT_MAIL has already been logged *today* to avoid duplicate sending.
        if is_sales:
            if EmailLog.objects.filter(task=task, recipient_email=employee.email, email_type="ALERT_MAIL", created_at__date=timezone.localdate()).exists():
                continue
        else:
            if EmailLog.objects.filter(task=task, recipient_email=employee.email, email_type="ALERT_MAIL").exists():
                continue

        if is_sales:
            subject = f"Follow-up Reminder: {task.task_name} - Follow-up Date: {task.due_date}"
            last_follow_up = task.follow_ups.first()
            last_discussion = last_follow_up.discussed_points if last_follow_up else "No previous follow-ups logged."
            last_follow_up_date = last_follow_up.follow_up_date.strftime("%B %d, %Y") if last_follow_up else "N/A"
            
            site_url = getattr(settings, 'SITE_URL', 'https://flowforceworkspace.cloud')
            task_link = f"{site_url}/tables/{task.row.table_id}/?open_task_id={task.id}"
            
            context = {
                'employee_name': employee.full_name or employee.email,
                'customer_name': task.task_name,
                'follow_up_date': str(task.due_date),
                'last_discussion': last_discussion,
                'last_follow_up_date': last_follow_up_date,
                'priority': task.priority,
                'assigned_by': task.assigned_by.full_name if task.assigned_by else 'System',
                'department': task.row.table.department.name if task.row.table.department else 'Global',
                'task_link': task_link,
            }
            
            html_message = render_to_string('emails/sales_alert_mail.html', context)
            
            email_log = EmailLog.objects.create(
                recipient_email=employee.email,
                subject=subject,
                body=html_message,
                task=task,
                email_type='ALERT_MAIL',
                status='PENDING',
                max_retries=2,
            )
        else:
            subject = f"Task: {task.task_name} - Due Date: {task.due_date}"
            body = f"Task Name: {task.task_name}\nDue Date: {task.due_date}"

            email_log = EmailLog.objects.create(
                recipient_email=employee.email,
                subject=subject,
                body=body,
                task=task,
                email_type='ALERT_MAIL',
                status='PENDING',
                max_retries=2,
            )

        send_email_log_task.delay(email_log.id)

@shared_task
def send_review_request_mail(task_id):
    """
    Trigger REVIEW_REQUEST_MAIL to admin.
    """
    return  # Disabled as per request

    recipients = []
    if task.assigned_by:
        recipients.append(task.assigned_by)
    else:
        if task.row.table.department:
            recipients = list(EmployeeUser.objects.filter(role__in=["ADMIN", "DEPARTMENT_ADMIN"], department=task.row.table.department))
        if not recipients:
            recipients = list(EmployeeUser.objects.filter(role="SUPER_ADMIN"))

    for recipient in recipients:
        if EmailLog.objects.filter(task=task, recipient_email=recipient.email, email_type="REVIEW_REQUEST_MAIL").exists():
            continue

        subject = f"Task Review Requested: {task.task_name}"
        site_url = getattr(settings, 'SITE_URL', 'https://flowforceworkspace.cloud')
        task_link = f"{site_url}/tables/{task.row.table_id}/?open_task_id={task.id}"
        employee_name = ", ".join([u.full_name for u in task.assigned_to.all()])

        context = {
            'admin_name': recipient.full_name,
            'employee_name': employee_name,
            'task_name': task.task_name,
            'due_date': str(task.due_date),
            'priority': task.priority,
            'task_link': task_link,
        }

        html_message = render_to_string('emails/review_request_mail.html', context)

        email_log = EmailLog.objects.create(
            recipient_email=recipient.email,
            subject=subject,
            body=html_message,
            task=task,
            email_type='REVIEW_REQUEST_MAIL',
            status='PENDING',
            max_retries=3,
        )

        send_email_log_task.delay(email_log.id)

@shared_task
def send_approval_status_mail(task_id):
    """
    Trigger APPROVAL_STATUS_MAIL to assigned employees.
    """
    return  # Disabled as per request

    last_comment = task.comments.order_by('-created_at').first()
    feedback = last_comment.content if last_comment else "No feedback provided."

    for employee in task.assigned_to.all():
        subject_map = {
            'APPROVED': f"Task Approved: {task.task_name}",
            'REJECTED': f"Task Rejected: {task.task_name}",
            'CHANGES_REQUESTED': f"Changes Requested: {task.task_name}",
        }
        subject = subject_map.get(task.status, f"Task Status Updated: {task.task_name}")
        site_url = getattr(settings, 'SITE_URL', 'https://flowforceworkspace.cloud')
        task_link = f"{site_url}/tables/{task.row.table_id}/?open_task_id={task.id}"

        context = {
            'employee_name': employee.full_name,
            'task_name': task.task_name,
            'status': task.status,
            'feedback': feedback,
            'task_link': task_link,
        }

        html_message = render_to_string('emails/approval_status_mail.html', context)

        email_log = EmailLog.objects.create(
            recipient_email=employee.email,
            subject=subject,
            body=html_message,
            task=task,
            email_type='APPROVAL_STATUS_MAIL',
            status='PENDING',
            max_retries=3,
        )

        send_email_log_task.delay(email_log.id)

@shared_task
def check_overdue_escalations():
    """
    Daily check for overdue tasks and escalate to higher roles.
    """
    today = timezone.localdate()
    tasks = Task.objects.filter(due_date__lt=today).exclude(status__in=['COMPLETED', 'APPROVED'])

    for task in tasks:
        days_overdue = (today - task.due_date).days
        
        recipients = []
        if days_overdue == 6:
            recipients = list(task.assigned_to.all())
        else:
            continue

        unique_recipients = []
        seen_emails = set()
        for r in recipients:
            if r.email and r.email not in seen_emails:
                seen_emails.add(r.email)
                unique_recipients.append(r)

        if unique_recipients:
            task.last_escalation_level = days_overdue
            task.last_escalation_at = timezone.now()
            task.save()

            for recipient in unique_recipients:
                subject = f"ESCALATION: Overdue Task - {task.task_name} ({days_overdue} days overdue)"
                site_url = getattr(settings, 'SITE_URL', 'https://flowforceworkspace.cloud')
                task_link = f"{site_url}/tables/{task.row.table_id}/?open_task_id={task.id}"

                context = {
                    'recipient_name': recipient.full_name,
                    'days': days_overdue,
                    'task_name': task.task_name,
                    'due_date': str(task.due_date),
                    'employee_name': ", ".join([u.full_name for u in task.assigned_to.all()]),
                    'department_name': task.row.table.department.name if task.row.table.department else "Global",
                    'status': task.status,
                    'priority': task.priority,
                    'task_link': task_link,
                }

                html_message = render_to_string('emails/overdue_escalation_mail.html', context)

                email_log = EmailLog.objects.create(
                    recipient_email=recipient.email,
                    subject=subject,
                    body=html_message,
                    task=task,
                    email_type='OVERDUE_ESCALATION_MAIL',
                    status='PENDING',
                    max_retries=3,
                )

                send_email_log_task.delay(email_log.id)

@shared_task
def retry_failed_emails():
    """
    Retry failed emails with exponential backoff
    """
    now = timezone.now()
    from django.db.models import F
    failed_emails = EmailLog.objects.filter(
        status='FAILED',
        retry_count__lt=F('max_retries'),
        next_retry_at__lte=now
    )

    for email_log in failed_emails:
        send_email_log_task.delay(email_log.id)
