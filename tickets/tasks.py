"""
Celery background tasks for Tickets & Synology NAS integration.
"""
import logging
import mimetypes
import os
import posixpath
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from .models import Ticket, TicketEvent, TicketFile
from .services import file_handler, synology, workflow

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def push_file_to_synology(self, ticket_file_id):
    """
    Push a locally stored TicketFile to the appropriate Synology NAS folder.
    """
    try:
        tf = TicketFile.objects.select_related("ticket").get(id=ticket_file_id)
    except TicketFile.DoesNotExist:
        logger.warning("TicketFile %s not found for Synology push.", ticket_file_id)
        return

    if not tf.local_temp_path or not os.path.exists(tf.local_temp_path):
        if tf.sync_status == TicketFile.SYNC_SYNCED:
            return
        tf.sync_status = TicketFile.SYNC_FAILED
        tf.sync_error = "Local source file not found."
        tf.save(update_fields=["sync_status", "sync_error"])
        return

    if not synology.is_configured():
        logger.info("Synology integration is not configured. Keeping file %s in PENDING status.", tf.id)
        tf.sync_status = TicketFile.SYNC_PENDING
        tf.save(update_fields=["sync_status"])
        return

    client = synology.get_synology_client()
    ticket = tf.ticket

    try:
        # Ensure root ticket folder exists on NAS
        if not ticket.nas_folder:
            ticket.nas_folder = client.create_ticket_folder_structure(ticket)
            ticket.save(update_fields=["nas_folder"])
        else:
            # Ensure subfolders exist
            client.create_ticket_folder_structure(ticket)

        # Target subfolder
        subfolder_name = TicketFile.CATEGORY_FOLDER_MAP.get(tf.category, "01 Client Details")
        target_folder = posixpath.join(ticket.nas_folder, subfolder_name)

        # Upload file to Synology NAS
        remote_path = client.upload_file(
            local_path=tf.local_temp_path,
            target_nas_folder=target_folder,
            filename=tf.stored_name or tf.original_name
        )

        tf.nas_path = remote_path
        tf.sync_status = TicketFile.SYNC_SYNCED
        tf.sync_error = ""
        tf.save(update_fields=["nas_path", "sync_status", "sync_error"])
        logger.info("Successfully synced TicketFile %s to %s", tf.id, remote_path)

    except (synology.SynologyNetworkError, synology.SynologyAuthError) as e:
        logger.warning("Synology transient error syncing file %s: %s", tf.id, e)
        tf.sync_status = TicketFile.SYNC_PENDING
        tf.sync_error = str(e)
        tf.save(update_fields=["sync_status", "sync_error"])
        # Retry with exponential backoff
        try:
            self.retry(exc=e)
        except Exception:
            pass

    except Exception as e:
        logger.error("Failed to sync file %s to Synology: %s", tf.id, e, exc_info=True)
        tf.sync_status = TicketFile.SYNC_FAILED
        tf.sync_error = str(e)
        tf.save(update_fields=["sync_status", "sync_error"])


@shared_task
def retry_pending_synology_uploads():
    """
    Periodic task (every 10m) to retry any files still in PENDING or FAILED status.
    """
    if not synology.is_configured():
        return

    pending_files = TicketFile.objects.filter(
        sync_status__in=[TicketFile.SYNC_PENDING, TicketFile.SYNC_FAILED],
        is_hidden=False
    ).exclude(local_temp_path="")

    count = 0
    for tf in pending_files[:50]:
        if tf.local_temp_path and os.path.exists(tf.local_temp_path):
            push_file_to_synology.delay(tf.id)
            count += 1

    if count:
        logger.info("Scheduled retry for %s pending TicketFiles to Synology.", count)


@shared_task
def process_zip_file_task(ticket_file_id, user_id=None):
    """
    Extract a ZIP archive in the background and queue all extracted items to Synology.
    """
    try:
        tf = TicketFile.objects.select_related("ticket").get(id=ticket_file_id)
    except TicketFile.DoesNotExist:
        return

    user = User.objects.filter(id=user_id).first() if user_id else None

    if not tf.local_temp_path or not os.path.exists(tf.local_temp_path):
        return

    try:
        extracted = file_handler.extract_zip_safely(
            zip_path=tf.local_temp_path,
            ticket=tf.ticket,
            base_category=tf.category,
            user=user
        )

        for new_file in extracted:
            push_file_to_synology.delay(new_file.id)

        # Log event on ticket
        TicketEvent.objects.create(
            ticket=tf.ticket,
            user=user,
            action="ZIP_EXTRACTED",
            note=f"Extracted {len(extracted)} files from {tf.original_name}."
        )

    except Exception as e:
        logger.error("ZIP extraction failed for file %s: %s", tf.id, e, exc_info=True)
        TicketEvent.objects.create(
            ticket=tf.ticket,
            user=user,
            action="ZIP_ERROR",
            note=f"Error extracting ZIP {tf.original_name}: {e}"
        )


@shared_task
def download_file_from_link_task(ticket_id, url, category, user_id=None):
    """
    Background download of file from link with SSRF checks, then push to Synology.
    """
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except Ticket.DoesNotExist:
        return

    user = User.objects.filter(id=user_id).first() if user_id else None

    try:
        temp_path, orig_name, mime, size = file_handler.download_url_safely(url)
        sha = file_handler.compute_sha256(temp_path)

        # If it is a ZIP archive, extract it
        if orig_name.lower().endswith(".zip") or mime in ("application/zip", "application/x-zip-compressed"):
            extracted = file_handler.extract_zip_safely(
                zip_path=temp_path,
                ticket=ticket,
                base_category=category,
                user=user
            )
            for f in extracted:
                push_file_to_synology.delay(f.id)

            TicketEvent.objects.create(
                ticket=ticket,
                user=user,
                action="LINK_ZIP_IMPORTED",
                note=f"Imported ZIP from {url[:100]} ({len(extracted)} files extracted)."
            )
            return

        # Skip duplicate
        if TicketFile.objects.filter(ticket=ticket, sha256=sha, is_hidden=False).exists():
            logger.info("Skipping duplicate downloaded file for ticket %s: %s", ticket.number, orig_name)
            os.remove(temp_path)
            return

        stored_name = file_handler.generate_stored_filename(ticket, category, orig_name)
        thumb = None
        if mime.startswith("image/"):
            thumb = file_handler.generate_thumbnail(temp_path)

        tf = TicketFile.objects.create(
            ticket=ticket,
            category=category,
            original_name=orig_name,
            stored_name=stored_name,
            size_bytes=size,
            mime_type=mime,
            sha256=sha,
            thumbnail=thumb,
            source=TicketFile.SOURCE_LINK,
            sync_status=TicketFile.SYNC_PENDING,
            local_temp_path=temp_path,
            uploaded_by=user,
        )

        push_file_to_synology.delay(tf.id)

        TicketEvent.objects.create(
            ticket=ticket,
            user=user,
            action="LINK_IMPORTED",
            note=f"Imported file {orig_name} from URL: {url[:120]}"
        )

    except Exception as e:
        logger.error("Download from link failed for ticket %s, url %s: %s", ticket_id, url, e)
        TicketEvent.objects.create(
            ticket=ticket,
            user=user,
            action="LINK_ERROR",
            note=f"Failed to download from link {url[:100]}: {e}"
        )


@shared_task
def scan_synology_ticket_folders():
    """
    Periodic task (every 15m) to scan Synology NAS folders of active tickets.
    Detects any files placed directly on NAS and indexes them in DB.
    """
    if not synology.is_configured():
        return

    client = synology.get_synology_client()
    active_tickets = Ticket.objects.filter(
        is_deleted=False
    ).exclude(nas_folder="").exclude(status__in=[Ticket.STATUS_CLOSED])

    for ticket in active_tickets[:20]:
        try:
            for cat_key, subfolder in TicketFile.CATEGORY_FOLDER_MAP.items():
                folder_path = posixpath.join(ticket.nas_folder, subfolder)
                items = client.list_folder(folder_path)

                for item in items:
                    if item.get("isdir"):
                        continue
                    remote_path = item.get("path")
                    filename = item.get("name")
                    file_size = item.get("size", 0)

                    # Check if already indexed
                    if TicketFile.objects.filter(ticket=ticket, nas_path=remote_path).exists():
                        continue

                    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

                    TicketFile.objects.create(
                        ticket=ticket,
                        category=cat_key,
                        original_name=filename,
                        stored_name=filename,
                        nas_path=remote_path,
                        size_bytes=file_size,
                        mime_type=mime,
                        source=TicketFile.SOURCE_NAS_SCAN,
                        sync_status=TicketFile.SYNC_SYNCED,
                        local_temp_path="",
                    )
                    logger.info("Indexed new NAS file for ticket %s: %s", ticket.number, remote_path)

        except Exception as e:
            logger.warning("Error scanning NAS folder for ticket %s: %s", ticket.number, e)


@shared_task
def check_expiring_warranties_daily():
    """
    Daily task (9:00 AM) to notify managers about project/service warranties expiring within 30 days.
    """
    today = timezone.localdate()
    in_30_days = today + timezone.timedelta(days=30)

    expiring_tickets = Ticket.objects.filter(
        is_deleted=False,
        warranty_end__gte=today,
        warranty_end__lte=in_30_days
    ).select_related("assigned_to", "created_by")

    if not expiring_tickets.exists():
        return

    managers = User.objects.filter(is_active=True).filter(
        models.Q(is_superuser=True) | models.Q(groups__name="Ticket Managers")
    ).distinct()

    summary_lines = []
    for t in expiring_tickets:
        days = (t.warranty_end - today).days
        summary_lines.append(f"• {t.number} - {t.title} ({t.customer_name}): {days} days remaining (ends {t.warranty_end})")

    body = f"The following warranties expire within the next 30 days:\n\n" + "\n".join(summary_lines)

    for m in managers:
        try:
            from tasks.models import Notification
            Notification.objects.create(
                user=m,
                title=f"Warranty Alert: {expiring_tickets.count()} tickets expiring soon",
                description=body[:500],
                type="ALERT",
                link="/tickets/?filter=expiring_warranty"
            )
        except Exception as e:
            logger.debug("Failed to create warranty alert notification for %s: %s", m, e)
