"""
File handling service: naming conventions, thumbnails, HEIC conversion, safe ZIP extraction, and SSRF-safe URL downloading.
"""
import hashlib
import ipaddress
import logging
import mimetypes
import os
import posixpath
import re
import socket
import tempfile
import urllib.parse
import zipfile
from io import BytesIO

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:
    HEIF_SUPPORTED = False

from ..models import Ticket, TicketFile
from . import synology

logger = logging.getLogger(__name__)

# SSRF Blocked subnets
PRIVATE_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / Cloud metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Zip Bomb Guard Constants
MAX_ZIP_FILES = 10000
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024 * 1024  # 20 GB
MAX_ZIP_RATIO = 200

# Category naming representation
CATEGORY_SLUG_MAP = {
    TicketFile.CATEGORY_CLIENT_DETAILS: "01-ClientDetails",
    TicketFile.CATEGORY_PHOTO: "02-Photos",
    TicketFile.CATEGORY_SITE_REPORT: "03-SiteReports",
    TicketFile.CATEGORY_WARRANTY: "04-Warranty",
    TicketFile.CATEGORY_RECEIPT: "05-Receipts",
    TicketFile.CATEGORY_OTHER: "Other",
}


def compute_sha256(file_path):
    """Compute sha256 checksum of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_stored_filename(ticket, category, original_filename):
    """
    Format: {JOB-1921 or TKT-26-0001}_{Category}_{YYYY-MM-DD}_{NNN}.{ext}
    """
    prefix = f"JOB-{ticket.job_no}" if ticket.job_no else ticket.number
    prefix = synology.sanitize_path_segment(prefix, max_length=20)

    cat_slug = CATEGORY_SLUG_MAP.get(category, "General")
    today_str = timezone.localdate().strftime("%Y-%m-%d")

    _, ext = os.path.splitext(original_filename)
    ext = ext.lower() if ext else ""

    # Determine sequence number for today
    today_count = TicketFile.objects.filter(
        ticket=ticket,
        uploaded_at__date=timezone.localdate()
    ).count() + 1

    stored_name = f"{prefix}_{cat_slug}_{today_str}_{today_count:03d}{ext}"
    return stored_name


def generate_thumbnail(file_path, max_size=(400, 400)):
    """
    Generate JPEG thumbnail for an image.
    Supports JPG, PNG, WEBP, and HEIC (if pillow_heif installed).
    Returns ContentFile or None.
    """
    try:
        with Image.open(file_path) as img:
            # Handle orientation if present
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass

            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            thumb_io = BytesIO()
            img.save(thumb_io, format="JPEG", quality=85, optimize=True)
            return ContentFile(thumb_io.getvalue(), name="thumb.jpg")
    except Exception as e:
        logger.debug("Could not generate thumbnail for %s: %s", file_path, e)
        return None


def convert_heic_to_jpeg(heic_path):
    """
    Convert a HEIC/HEIF file to JPEG if HEIF is supported.
    Returns path to converted JPEG or None.
    """
    if not HEIF_SUPPORTED:
        return None

    try:
        base, _ = os.path.splitext(heic_path)
        jpg_path = f"{base}_converted.jpg"
        with Image.open(heic_path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(jpg_path, format="JPEG", quality=90)
        return jpg_path
    except Exception as e:
        logger.warning("Failed to convert HEIC to JPEG: %s", e)
        return None


def validate_url_security(url):
    """
    SSRF Protection:
    - Scheme must be http or https.
    - Hostname must resolve to a valid IP.
    - IP must not be private, loopback, link-local, multicast, or in cloud metadata ranges.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError("Only HTTP and HTTPS links are allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise ValidationError("Invalid URL: missing hostname.")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValidationError(f"Could not resolve hostname '{hostname}': {e}")

    for addr in addr_infos:
        ip_str = addr[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            raise ValidationError(f"Access to private or local network addresses ({ip_str}) is blocked.")

        for net in PRIVATE_IP_RANGES:
            if ip in net:
                raise ValidationError(f"Access to restricted IP address {ip_str} is blocked.")

    return True


def download_url_safely(url, target_dir=None):
    """
    Download a file from an external URL with SSRF protection and WeChat expiry detection.
    Returns (temp_path, original_filename, mime_type, file_size).
    """
    validate_url_security(url)

    max_mb = getattr(settings, "TICKETS_MAX_UPLOAD_MB", 5120)
    max_bytes = max_mb * 1024 * 1024

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 FlowForceTickets/1.0"
    }

    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ValidationError(f"Could not download file from URL: {e}")

    # Validate redirect destination IP for SSRF
    validate_url_security(resp.url)

    content_type = resp.headers.get("Content-Type", "").lower()
    content_disp = resp.headers.get("Content-Disposition", "")

    # Extract filename
    filename = "downloaded_file"
    if "filename=" in content_disp:
        parts = content_disp.split("filename=")
        if len(parts) > 1:
            raw_fn = parts[1].split(";")[0].strip(' "\'')
            if raw_fn:
                filename = raw_fn
    else:
        path_name = posixpath.basename(urllib.parse.urlparse(resp.url).path)
        if path_name:
            filename = path_name

    temp_dir = target_dir or tempfile.gettempdir()
    temp_file = tempfile.NamedTemporaryFile(dir=temp_dir, delete=False)
    downloaded_size = 0

    first_chunk = True
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                if first_chunk:
                    first_chunk = False
                    # Check for WeChat / Weixin expired or login page HTML
                    peek = chunk[:2048].lower()
                    if (
                        b"weixin" in peek or b"wechat" in peek
                        or b"login" in peek or b"expired" in peek
                        or b"<!doctype html>" in peek or b"<html" in peek
                    ) and not filename.lower().endswith((".html", ".htm")):
                        if "text/html" in content_type:
                            raise ValidationError(
                                "This link has expired or requires WeChat login. "
                                "Please download the file on your device and upload it directly."
                            )

                downloaded_size += len(chunk)
                if downloaded_size > max_bytes:
                    raise ValidationError(f"File exceeds maximum allowed upload size of {max_mb} MB.")
                temp_file.write(chunk)
    except Exception:
        temp_file.close()
        if os.path.exists(temp_file.name):
            os.remove(temp_file.name)
        raise

    temp_file.close()
    mime = mimetypes.guess_type(filename)[0] or content_type.split(";")[0] or "application/octet-stream"
    return temp_file.name, filename, mime, downloaded_size


def extract_zip_safely(zip_path, ticket, base_category, user=None):
    """
    Safely extract a ZIP archive:
    - Zip Slip protection (ensures paths don't traverse outside extraction root).
    - Zip Bomb protection (checks file count, ratio, total uncompressed size).
    - Duplicate detection via sha256 (skips existing files).
    - Categorization: images go to 02 Photos, PDFs to base_category.
    Returns list of created TicketFile instances.
    """
    if not zipfile.is_zipfile(zip_path):
        raise ValidationError("The provided file is not a valid ZIP archive.")

    created_files = []
    temp_extract_dir = tempfile.mkdtemp(prefix="ticket_zip_")

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infolist = zf.infolist()
            total_files = len(infolist)
            total_uncompressed = sum(info.file_size for info in infolist)
            compressed_size = os.path.getsize(zip_path) or 1

            if total_files > MAX_ZIP_FILES:
                raise ValidationError(f"ZIP contains {total_files} files (maximum {MAX_ZIP_FILES} allowed).")

            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ValidationError(f"ZIP uncompressed size exceeds limit ({(total_uncompressed / (1024**3)):.1f} GB).")

            ratio = total_uncompressed / compressed_size
            if ratio > MAX_ZIP_RATIO and total_uncompressed > 50 * 1024 * 1024:
                raise ValidationError(f"Suspicious compression ratio ({ratio:.1f}:1). Potential ZIP bomb.")

            for info in infolist:
                if info.is_dir():
                    continue

                # Zip Slip Protection
                norm_name = posixpath.normpath(info.filename)
                if norm_name.startswith("/") or norm_name.startswith("\\") or ".." in norm_name:
                    logger.warning("Skipping potentially unsafe path in ZIP: %s", info.filename)
                    continue

                extracted_dest = os.path.join(temp_extract_dir, synology.sanitize_path_segment(os.path.basename(norm_name)))

                with zf.open(info) as src, open(extracted_dest, "wb") as dst:
                    for chunk in iter(lambda: src.read(65536), b""):
                        dst.write(chunk)

                file_size = os.path.getsize(extracted_dest)
                sha = compute_sha256(extracted_dest)

                # Skip duplicates for this ticket
                if TicketFile.objects.filter(ticket=ticket, sha256=sha, is_hidden=False).exists():
                    logger.info("Skipping duplicate file in ticket %s: %s (sha: %s)", ticket.number, info.filename, sha[:8])
                    os.remove(extracted_dest)
                    continue

                orig_filename = os.path.basename(info.filename)
                ext = os.path.splitext(orig_filename)[1].lower()
                mime = mimetypes.guess_type(orig_filename)[0] or "application/octet-stream"

                # Automatic category assignment
                if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif") or mime.startswith("image/"):
                    category = TicketFile.CATEGORY_PHOTO
                elif ext == ".pdf":
                    category = base_category if base_category != TicketFile.CATEGORY_PHOTO else TicketFile.CATEGORY_SITE_REPORT
                else:
                    category = base_category or TicketFile.CATEGORY_OTHER

                stored_filename = generate_stored_filename(ticket, category, orig_filename)

                # Create thumbnail if image
                thumb = None
                if mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
                    thumb = generate_thumbnail(extracted_dest)

                tf = TicketFile.objects.create(
                    ticket=ticket,
                    category=category,
                    original_name=orig_filename,
                    stored_name=stored_filename,
                    size_bytes=file_size,
                    mime_type=mime,
                    sha256=sha,
                    thumbnail=thumb,
                    source=TicketFile.SOURCE_ZIP,
                    sync_status=TicketFile.SYNC_PENDING,
                    local_temp_path=extracted_dest,
                    uploaded_by=user,
                )
                created_files.append(tf)

    finally:
        # Note: Do not remove temp_extract_dir immediately since local_temp_path points to files inside it
        # They will be cleaned up by push_file_to_synology after sync completes.
        pass

    return created_files

