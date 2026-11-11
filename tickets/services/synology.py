"""
Synology File Station Web API client for Flow-Force Workspace.

Connects to Synology NAS (e.g. over Tailscale / LAN) using FileStation Web API.
Strict safety rules:
- NEVER overwrite existing files.
- NEVER provide a file deletion method (hard delete is disabled on Synology).
- Always validate and sanitize folder and file paths against directory traversal.
- Gracefully degrades when NAS configuration is missing or NAS is offline.
"""
import logging
import os
import posixpath
import re
import urllib.parse
from datetime import timedelta
import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

SUBFOLDERS = [
    "01 Client Details",
    "02 Photos",
    "03 Site Reports",
    "04 Warranty",
    "05 Receipts",
]


class SynologyError(Exception):
    """Base exception for Synology operations."""
    pass


class SynologyNotConfigured(SynologyError):
    """Raised when Synology credentials or URL are not configured."""
    pass


class SynologyAuthError(SynologyError):
    """Raised on authentication failures."""
    pass


class SynologyNetworkError(SynologyError):
    """Raised on connection timeout or unreachable host."""
    pass


def is_configured():
    """Check if Synology integration is configured in settings."""
    url = getattr(settings, "SYNOLOGY_URL", "")
    user = getattr(settings, "SYNOLOGY_USER", "")
    pwd = getattr(settings, "SYNOLOGY_PASSWORD", "")
    return bool(url and user and pwd)


def sanitize_path_segment(name, max_length=80):
    """
    Sanitize a directory or file name segment:
    - Strips directory traversal dots (..)
    - Strips illegal Windows/Linux characters: / \\ : * ? " < > |
    - Strips leading/trailing spaces and dots
    - Collapses multiple underscores/spaces
    - Enforces max_length
    """
    if not name:
        return "unnamed"
    cleaned = str(name)
    # Remove directory traversal dots
    cleaned = re.sub(r'\.{2,}', '', cleaned)
    # Replace illegal characters with underscore
    cleaned = re.sub(r'[/\\:*?"<>|]', '_', cleaned)
    # Replace control chars
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    # Strip leading/trailing dots and spaces
    cleaned = cleaned.strip(". ")
    # Collapse consecutive whitespace and underscores
    cleaned = re.sub(r'[\s_]+', '_', cleaned)
    cleaned = cleaned.strip("_.")
    if not cleaned:
        cleaned = "unnamed"
    return cleaned[:max_length]


def build_ticket_nas_folder(ticket):
    """
    Build the full canonical NAS folder path for a ticket.
    Guarantees no traversal and enforces root prefix.
    Format:
      - If ticket has job_no: <SYNOLOGY_ROOT>/JOB-<job_no>_<customer>_<title>
      - Else if enquiry_number: <SYNOLOGY_ROOT>/ENQ-<enquiry>_<customer>_<title>
      - Else: <SYNOLOGY_ROOT>/<ticket.number>_<customer>_<title>
    """
    root = (getattr(settings, "SYNOLOGY_ROOT", "") or "/FlowForce/Jobs").strip()
    if not root.startswith("/"):
        root = "/" + root
    root = posixpath.normpath(root)

    cust = sanitize_path_segment(ticket.customer_name or "General", max_length=25)
    title = sanitize_path_segment(ticket.title or "Ticket", max_length=30)

    if ticket.job_no:
        job = sanitize_path_segment(ticket.job_no, max_length=20)
        segment = f"JOB-{job}_{cust}_{title}"
    elif ticket.enquiry_number:
        enq = sanitize_path_segment(ticket.enquiry_number.replace("/", "-").replace("\\", "-"), max_length=25)
        segment = f"ENQ-{enq}_{cust}_{title}"
    else:
        segment = f"{ticket.number}_{cust}_{title}"

    segment = sanitize_path_segment(segment, max_length=80)
    folder_path = posixpath.join(root, segment)
    return folder_path


class SynologyClient:
    """
    Client for Synology File Station Web API.
    Handles session authentication, caching SID, and token renewal on expiration.
    """

    def __init__(self, url=None, user=None, password=None, root=None, verify_ssl=None):
        self.url = (url or getattr(settings, "SYNOLOGY_URL", "")).rstrip("/")
        self.user = user or getattr(settings, "SYNOLOGY_USER", "")
        self.password = password or getattr(settings, "SYNOLOGY_PASSWORD", "")
        self.root = (root or getattr(settings, "SYNOLOGY_ROOT", "") or "/FlowForce/Jobs").rstrip("/")
        self.verify_ssl = getattr(settings, "SYNOLOGY_VERIFY_SSL", False) if verify_ssl is None else verify_ssl
        self.session = requests.Session()

    def _cache_key(self):
        return f"synology_sid_{self.user}"

    def get_cached_sid(self):
        return cache.get(self._cache_key())

    def set_cached_sid(self, sid):
        # Cache for 2 hours (Synology sessions typically last 24h or till logout)
        cache.set(self._cache_key(), sid, timeout=7200)

    def clear_cached_sid(self):
        cache.delete(self._cache_key())

    def login(self, force=False):
        """Authenticate with Synology DSM and obtain session ID (SID)."""
        if not self.url or not self.user or not self.password:
            raise SynologyNotConfigured("Synology URL, user, or password is not configured.")

        if not force:
            cached = self.get_cached_sid()
            if cached:
                return cached

        auth_url = f"{self.url}/webapi/auth.cgi"
        params = {
            "api": "SYNO.API.Auth",
            "version": "3",
            "method": "login",
            "account": self.user,
            "passwd": self.password,
            "session": "FileStation",
            "format": "sid",
        }

        try:
            resp = self.session.get(auth_url, params=params, verify=self.verify_ssl, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning("Synology auth request failed: %s", e)
            raise SynologyNetworkError(f"Could not reach Synology NAS at {self.url}: {e}")
        except Exception as e:
            logger.warning("Failed to parse Synology auth response: %s", e)
            raise SynologyAuthError(f"Invalid auth response from Synology: {e}")

        if not data.get("success"):
            error_info = data.get("error", {})
            code = error_info.get("code")
            msg = f"Synology authentication failed (error code {code})"
            if code == 400:
                msg = "No such account or incorrect password for Synology."
            elif code == 401:
                msg = "Synology account is disabled."
            elif code == 402:
                msg = "Synology permission denied."
            elif code == 403:
                msg = "Synology 2-step verification code required."
            logger.error(msg)
            raise SynologyAuthError(msg)

        sid = data.get("data", {}).get("sid")
        if not sid:
            raise SynologyAuthError("Synology did not return a session ID (sid).")

        self.set_cached_sid(sid)
        return sid

    def _api_request(self, api, method, version="2", cgi="entry.cgi", params=None, data=None, files=None, retry_auth=True):
        """Internal helper for File Station requests with automatic session refresh."""
        sid = self.login()
        url = f"{self.url}/webapi/{cgi}"

        req_params = dict(params or {})
        req_params["api"] = api
        req_params["version"] = version
        req_params["method"] = method
        req_params["_sid"] = sid

        try:
            if files:
                # Multipart upload request
                resp = self.session.post(url, params=req_params, data=data, files=files, verify=self.verify_ssl, timeout=600)
            elif data:
                resp = self.session.post(url, params=req_params, data=data, verify=self.verify_ssl, timeout=60)
            else:
                resp = self.session.get(url, params=req_params, verify=self.verify_ssl, timeout=60)

            resp.raise_for_status()
            res_json = resp.json()
        except requests.RequestException as e:
            logger.warning("Synology API call %s.%s failed: %s", api, method, e)
            raise SynologyNetworkError(f"Synology connection error: {e}")

        if not res_json.get("success"):
            error = res_json.get("error", {})
            code = error.get("code", 0)

            # Error codes 105, 106, 119 indicate invalid/expired session
            if code in (105, 106, 119) and retry_auth:
                logger.info("Synology session expired (code %s). Re-authenticating...", code)
                self.clear_cached_sid()
                return self._api_request(
                    api=api,
                    method=method,
                    version=version,
                    cgi=cgi,
                    params=params,
                    data=data,
                    files=files,
                    retry_auth=False
                )

            raise SynologyError(f"Synology error {code} during {api}.{method}: {error}")

        return res_json.get("data", {})

    def create_folder(self, parent_folder, name):
        """Create a single folder inside parent_folder."""
        return self._api_request(
            api="SYNO.FileStation.CreateFolder",
            method="create",
            version="2",
            cgi="entry.cgi",
            params={
                "folder_path": parent_folder,
                "name": name,
                "create_parents": "true",
            }
        )

    def create_ticket_folder_structure(self, ticket):
        """
        Create the full folder structure for a ticket on Synology NAS:
          - Ticket Root Folder
            - 01 Client Details
            - 02 Photos
            - 03 Site Reports
            - 04 Warranty
            - 05 Receipts
        Returns the nas_folder path.
        """
        folder_path = build_ticket_nas_folder(ticket)
        if not is_configured():
            logger.info("Synology is not configured. Returning folder path %s without NAS creation.", folder_path)
            return folder_path

        try:
            parent_dir, folder_name = posixpath.split(folder_path)
            # Create the main ticket directory
            try:
                self.create_folder(parent_dir, folder_name)
            except SynologyError as e:
                # Code 1003 often means folder already exists, which is acceptable
                logger.debug("Ticket folder create notice: %s", e)

            # Create standard subdirectories
            for sub in SUBFOLDERS:
                try:
                    self.create_folder(folder_path, sub)
                except SynologyError as e:
                    logger.debug("Subfolder %s create notice: %s", sub, e)

            return folder_path
        except Exception as e:
            logger.error("Failed to create ticket folder on Synology for %s: %s", ticket.number, e)
            raise

    def upload_file(self, local_path, target_nas_folder, filename):
        """
        Upload a local file to a specific folder on the Synology NAS.
        NEVER overwrites: overwrite=false.
        Returns the full remote path.
        """
        if not is_configured():
            raise SynologyNotConfigured("Synology NAS integration is not configured.")

        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file does not exist: {local_path}")

        file_size = os.path.getsize(local_path)
        safe_filename = sanitize_path_segment(filename)

        with open(local_path, "rb") as f:
            files = {
                "file": (safe_filename, f, "application/octet-stream")
            }
            data = {
                "path": target_nas_folder,
                "create_parents": "true",
                "overwrite": "false",
            }

            self._api_request(
                api="SYNO.FileStation.Upload",
                method="upload",
                version="2",
                cgi="entry.cgi",
                data=data,
                files=files
            )

        remote_path = posixpath.join(target_nas_folder, safe_filename)
        logger.info("Successfully uploaded %s (%s bytes) to %s", safe_filename, file_size, remote_path)
        return remote_path

    def list_folder(self, nas_folder):
        """
        List files and folders in a NAS directory.
        Returns a list of dicts with name, path, isdir, size, mtime.
        """
        if not is_configured():
            return []

        try:
            data = self._api_request(
                api="SYNO.FileStation.List",
                method="list",
                version="2",
                cgi="entry.cgi",
                params={
                    "folder_path": nas_folder,
                    "additional": "size,time,type",
                }
            )
            files = data.get("files", [])
            results = []
            for item in files:
                results.append({
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "isdir": item.get("isdir", False),
                    "size": item.get("additional", {}).get("size", 0),
                    "mtime": item.get("additional", {}).get("time", {}).get("mtime", 0),
                })
            return results
        except Exception as e:
            logger.warning("Failed to list NAS folder %s: %s", nas_folder, e)
            return []

    def download_file_stream(self, nas_path):
        """
        Download a file from the Synology NAS as a streaming response.
        """
        if not is_configured():
            raise SynologyNotConfigured("Synology NAS integration is not configured.")

        sid = self.login()
        url = f"{self.url}/webapi/entry.cgi"
        params = {
            "api": "SYNO.FileStation.Download",
            "version": "2",
            "method": "download",
            "path": nas_path,
            "mode": "open",
            "_sid": sid,
        }

        resp = self.session.get(url, params=params, verify=self.verify_ssl, stream=True, timeout=120)
        resp.raise_for_status()
        return resp

    def create_share_link(self, nas_path, days=7):
        """
        Create a shareable download link for a file or folder on the NAS.
        Returns dict with link URL and expiration date.
        """
        if not is_configured():
            raise SynologyNotConfigured("Synology NAS integration is not configured.")

        expire_date = (timezone.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        try:
            data = self._api_request(
                api="SYNO.FileStation.Sharing",
                method="create",
                version="3",
                cgi="entry.cgi",
                params={
                    "path": nas_path,
                    "date_expired": expire_date,
                }
            )
            links = data.get("links", [])
            if links:
                return {
                    "url": links[0].get("url"),
                    "expires_at": expire_date,
                }
        except Exception as e:
            logger.warning("Sharing API v3 failed (%s), attempting v1...", e)

        # Fallback to version 1
        data = self._api_request(
            api="SYNO.FileStation.Sharing",
            method="create",
            version="1",
            cgi="entry.cgi",
            params={
                "path": nas_path,
                "date_expired": expire_date,
            }
        )
        links = data.get("links", [])
        return {
            "url": links[0].get("url") if links else "",
            "expires_at": expire_date,
        }


def get_synology_client():
    """Factory function returning a configured SynologyClient instance."""
    return SynologyClient()
