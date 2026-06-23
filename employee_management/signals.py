"""
Signals for Employee Management App

Automatically handle audit logging, profile picture management,
and other system-wide employee events.
"""

from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone
from PIL import Image
from io import BytesIO
from django.core.files.base import ContentFile

from auth_app.models import EmployeeUser
from .models import (
    EmployeeActivityLog,
    EmployeeProfilePicture,
    EmployeeApprovalQueue
)


@receiver(post_save, sender=EmployeeUser)
def handle_employee_approval_workflow(sender, instance, created, **kwargs):
    """
    Handle automatic workflow when employee status changes.
    """
    
    if created:
        # New employee created - will be handled in service layer
        pass
    else:
        # Check if status changed to APPROVED
        try:
            old_instance = EmployeeUser.objects.get(pk=instance.pk)
        except EmployeeUser.DoesNotExist:
            return
        
        # If status just changed to APPROVED
        if old_instance.status != instance.status and instance.status == "APPROVED":
            # Automatically create/update approval queue
            try:
                approval = EmployeeApprovalQueue.objects.get(employee=instance)
                if not approval.is_approved:
                    approval.is_approved = True
                    approval.reviewed_at = timezone.now()
                    approval.save()
            except EmployeeApprovalQueue.DoesNotExist:
                # Create new queue entry if it doesn't exist
                EmployeeApprovalQueue.objects.create(
                    employee=instance,
                    is_approved=True,
                    reviewed_at=timezone.now()
                )


@receiver(post_save, sender=EmployeeProfilePicture)
def optimize_profile_picture(sender, instance, created, **kwargs):
    """
    Automatically optimize uploaded profile pictures.
    Resizes and compresses images to save storage.
    """
    
    if not instance.image:
        return
    
    try:
        # Only optimize if new or if image changed
        if not created:
            # Check if image actually changed
            try:
                old_instance = EmployeeProfilePicture.objects.get(pk=instance.pk)
                if old_instance.image == instance.image:
                    return
            except EmployeeProfilePicture.DoesNotExist:
                return
        
        # Open the image
        img = Image.open(instance.image)
        
        # Convert to RGB if necessary (for PNG with transparency, etc)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create a white background
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        
        # Resize to standard profile picture size (400x400)
        img.thumbnail((400, 400), Image.Resampling.LANCZOS)
        
        # Save optimized image
        output = BytesIO()
        img.save(output, format='JPEG', quality=85, optimize=True)
        output.seek(0)
        
        # Save the optimized image back
        instance.image.save(
            instance.image.name,
            ContentFile(output.getvalue()),
            save=False
        )
        
        # Save without triggering this signal again
        EmployeeProfilePicture.objects.filter(pk=instance.pk).update(
            image=instance.image
        )
    
    except Exception as e:
        # Log error but don't break the upload
        print(f"Error optimizing profile picture: {str(e)}")


@receiver(post_save, sender=EmployeeProfilePicture)
def mark_old_pictures_as_archived(sender, instance, created, **kwargs):
    """
    When a new profile picture is set as current,
    mark all other pictures as archived.
    """
    
    if created and instance.is_current:
        # Mark all other pictures for this employee as not current
        EmployeeProfilePicture.objects.filter(
            employee=instance.employee
        ).exclude(
            pk=instance.pk
        ).update(is_current=False)


@receiver(post_delete, sender=EmployeeProfilePicture)
def cleanup_profile_picture_file(sender, instance, **kwargs):
    """
    Delete the physical image file when profile picture is deleted.
    """
    
    if instance.image:
        # Delete the file from storage
        storage, path = instance.image.storage, instance.image.name
        if storage.exists(path):
            storage.delete(path)


@receiver(pre_save, sender=EmployeeUser)
def prepare_activity_log_for_changes(sender, instance, **kwargs):
    """
    Track what fields changed before saving.
    This data is used by service layer to log changes.
    """
    
    # Store original instance for comparison
    try:
        old_instance = EmployeeUser.objects.get(pk=instance.pk)
        instance._old_instance = old_instance
    except EmployeeUser.DoesNotExist:
        instance._old_instance = None
