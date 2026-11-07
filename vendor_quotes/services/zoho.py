"""
Zoho Books connector (OAuth 2.0, server-based client). Uses only the standard library.

Configuration (.env):
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET   - from api-console.zoho.com (Server-based Application)
  ZOHO_ACCOUNTS_URL                    - default https://accounts.zoho.com
  ZOHO_ORG_ID                          - default 741367552 (PT. Flow Force Indonesia)
  ZOHO_REDIRECT_URI                    - default <SITE_URL>/vendor-quotes/zoho/callback/

Until the client id/secret are set and an admin clicks "Connect Zoho", approved
quotes wait in 'Waiting for Zoho connection' and can be retried later.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import ZohoConnection

SCOPES = ",".join([
    "ZohoBooks.estimates.CREATE",
    "ZohoBooks.estimates.READ",
    "ZohoBooks.estimates.UPDATE",
    "ZohoBooks.contacts.READ",
])


class ZohoError(Exception):
    pass


class ZohoNotConnected(ZohoError):
    pass


def cfg(name, default=""):
    return getattr(settings, name, default) or default


def is_configured():
    return bool(cfg("ZOHO_CLIENT_ID") and cfg("ZOHO_CLIENT_SECRET"))


def redirect_uri():
    site = cfg("SITE_URL", "https://flowforceworkspace.cloud").rstrip("/")
    return cfg("ZOHO_REDIRECT_URI", f"{site}/vendor-quotes/zoho/callback/")


def accounts_url():
    return cfg("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/")


def org_id():
    return str(cfg("ZOHO_ORG_ID", "741367552"))


def authorize_url(state):
    q = urllib.parse.urlencode({
        "scope": SCOPES, "client_id": cfg("ZOHO_CLIENT_ID"), "response_type": "code",
        "access_type": "offline", "prompt": "consent", "redirect_uri": redirect_uri(), "state": state,
    })
    return f"{accounts_url()}/oauth/v2/auth?{q}"


def _http(method, url, data=None, headers=None, timeout=30):
    body = None
    headers = dict(headers or {})
    if data is not None:
        if isinstance(data, dict) and headers.get("Content-Type") == "application/json":
            body = json.dumps(data).encode()
        else:
            body = urllib.parse.urlencode(data).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"message": raw[:300]}
        raise ZohoError(payload.get("message") or payload.get("error") or f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise ZohoError(f"Could not reach Zoho: {e.reason}")
    try:
        return json.loads(raw)
    except ValueError:
        raise ZohoError(f"Unexpected Zoho response: {raw[:200]}")


def exchange_code(code, user):
    data = _http("POST", f"{accounts_url()}/oauth/v2/token", {
        "grant_type": "authorization_code", "client_id": cfg("ZOHO_CLIENT_ID"),
        "client_secret": cfg("ZOHO_CLIENT_SECRET"), "redirect_uri": redirect_uri(), "code": code,
    })
    if "refresh_token" not in data:
        raise ZohoError(data.get("error") or "Zoho did not return a refresh token. Try connecting again.")
    conn = ZohoConnection.get()
    conn.refresh_token = data["refresh_token"]
    conn.access_token = data.get("access_token", "")
    conn.access_token_expires_at = timezone.now() + timedelta(seconds=int(data.get("expires_in", 3600)) - 120)
    conn.api_domain = data.get("api_domain") or "https://www.zohoapis.com"
    conn.connected_by = user
    conn.connected_at = timezone.now()
    conn.save()
    return conn


def disconnect():
    conn = ZohoConnection.get()
    conn.refresh_token = conn.access_token = ""
    conn.access_token_expires_at = None
    conn.save()


def _token():
    if not is_configured():
        raise ZohoNotConnected("Zoho API keys are not set on the server yet.")
    conn = ZohoConnection.get()
    if not conn.is_connected:
        raise ZohoNotConnected("Zoho Books is not connected yet. An admin needs to click 'Connect Zoho'.")
    if conn.access_token and conn.access_token_expires_at and conn.access_token_expires_at > timezone.now():
        return conn
    data = _http("POST", f"{accounts_url()}/oauth/v2/token", {
        "grant_type": "refresh_token", "client_id": cfg("ZOHO_CLIENT_ID"),
        "client_secret": cfg("ZOHO_CLIENT_SECRET"), "refresh_token": conn.refresh_token,
    })
    if "access_token" not in data:
        raise ZohoError(data.get("error") or "Could not refresh the Zoho token. Reconnect Zoho.")
    conn.access_token = data["access_token"]
    conn.access_token_expires_at = timezone.now() + timedelta(seconds=int(data.get("expires_in", 3600)) - 120)
    if data.get("api_domain"):
        conn.api_domain = data["api_domain"]
    conn.save()
    return conn


def api(method, path, params=None, payload=None):
    conn = _token()
    params = dict(params or {})
    params["organization_id"] = org_id()
    url = f"{conn.api_domain.rstrip('/')}/books/v3/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Zoho-oauthtoken {conn.access_token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    data = _http(method, url, payload, headers)
    if data.get("code") not in (0, None):
        raise ZohoError(data.get("message") or f"Zoho error code {data.get('code')}")
    return data


def search_customers(text):
    data = api("GET", "contacts", {"contact_type": "customer", "contact_name_contains": text, "per_page": 20})
    return [{"id": c["contact_id"], "name": c.get("contact_name", "")} for c in data.get("contacts", [])]


def resolve_customer(enquiry):
    if enquiry.zoho_customer_id:
        return enquiry.zoho_customer_id
    matches = search_customers(enquiry.customer_name)
    exact = [m for m in matches if m["name"].strip().lower() == enquiry.customer_name.strip().lower()]
    pick = exact if exact else matches
    if len(pick) == 1:
        enquiry.zoho_customer_id = pick[0]["id"]
        enquiry.save(update_fields=["zoho_customer_id"])
        return pick[0]["id"]
    if not pick:
        raise ZohoError(f"Customer '{enquiry.customer_name}' was not found in Zoho Books. Pick the Zoho customer on the enquiry page.")
    raise ZohoError(f"{len(pick)} Zoho customers match '{enquiry.customer_name}'. Pick the right one on the enquiry page.")


def revision_number_for(base_number, revision_no):
    """26000231 -> 26000231R1; FFI_26000278R1 -> FFI_26000278R2 (always from the original number)."""
    base = base_number
    if "R" in base_number.upper():
        head, _, tail = base_number.upper().rpartition("R")
        if tail.isdigit() and head:
            base = base_number[:len(head)]
    return f"{base}R{revision_no}"


def create_draft_estimate(*, customer_id, reference, lines, estimate_number=None, notes=""):
    line_items = []
    for ln in lines:
        name = ln["part"] or ln["description"][:100]
        item = {"name": name[:100], "description": (ln["description"] if ln["part"] else "")[:2000],
                "quantity": ln["qty"], "rate": ln["rate"]}
        if ln.get("unit"):
            item["unit"] = ln["unit"][:20]
        line_items.append(item)
    payload = {"customer_id": customer_id, "reference_number": reference[:50],
               "date": timezone.localdate().isoformat(), "line_items": line_items}
    if notes:
        payload["notes"] = notes[:2000]
    params = {}
    if estimate_number:
        payload["estimate_number"] = estimate_number
        params["ignore_auto_number_generation"] = "true"
    data = api("POST", "estimates", params, payload)
    est = data.get("estimate") or {}
    if not est.get("estimate_id"):
        raise ZohoError("Zoho did not return the new quotation.")
    return est["estimate_id"], est.get("estimate_number", "")


def estimate_url(estimate_id):
    return f"https://books.zoho.com/app/{org_id()}#/quotes/{estimate_id}"
