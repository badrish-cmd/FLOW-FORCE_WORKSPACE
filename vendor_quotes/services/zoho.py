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
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import ZohoConnection

logger = logging.getLogger(__name__)

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


ORG_DEFAULTS = {"FFI": ("ZOHO_ORG_ID", "741367552"), "FFE": ("ZOHO_ORG_ID_FFE", "887677172")}
# Companies whose Zoho Books has no automatic quotation numbering: the workspace gives the number.
MANUAL_NUMBER_PREFIX = {"FFE": "FFE"}


def org_id(company=None):
    from ..company import clean, current
    key, default = ORG_DEFAULTS[clean(company or current())]
    return str(cfg(key, default))


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
    # Make sure this Zoho login really has the company's organisation, so FFI and FFE never get mixed up.
    from ..company import NAMES, current
    api_domain = data.get("api_domain") or "https://www.zohoapis.com"
    orgs = _http("GET", f"{api_domain.rstrip('/')}/books/v3/organizations", None,
                 {"Authorization": f"Zoho-oauthtoken {data.get('access_token', '')}"}).get("organizations") or []
    want = org_id()
    match = next((o for o in orgs if str(o.get("organization_id")) == want), None)
    if not match:
        names = ", ".join(f"{o.get('name')} ({o.get('organization_id')})" for o in orgs) or "none"
        raise ZohoError(f"This Zoho login has no access to {NAMES[current()]} (organisation {want}). "
                        f"It has: {names}. Sign in with the {current()} Zoho account and try again.")
    conn = ZohoConnection.get()
    conn.organization_name = match.get("name", "")[:200]
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
        from ..company import current
        raise ZohoNotConnected(f"Zoho Books ({current()}) is not connected yet. An admin needs to click "
                               f"'Connect Zoho' for {current()} on the Zoho Books page.")
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


class EstimateResult(tuple):
    """
    Subclass of tuple (estimate_id, estimate_number) that also holds stick_warnings.
    Unpacks as a 2-tuple (id, number) for backward compatibility with existing tests.
    """
    def __new__(cls, est_id, est_no, stick_warnings=None):
        return super().__new__(cls, (est_id, est_no))

    def __init__(self, est_id, est_no, stick_warnings=None):
        self.estimate_id = est_id
        self.estimate_number = est_no
        self.stick_warnings = stick_warnings or []


def build_line_description(*, description="", part="", spec="", commodity_code="", edited_description=None):
    """
    Build the standard Zoho line item description text.
    Line 1: 'Supply of ' + description + (' - ' + part if part)  (do not double 'Supply of' if already present)
    Line 2: model / spec / make text if present
    Line 3: 'Commodity Code: ' + code (only if present)
    If edited_description is provided and non-empty, use that edited text exactly.
    """
    if edited_description is not None and str(edited_description).strip():
        return str(edited_description).strip()

    lines = []
    desc = (description or "").strip()
    p = (part or "").strip()

    if desc:
        if desc.lower().startswith("supply of "):
            l1 = desc
        elif desc.lower() == "supply of":
            l1 = desc
        else:
            l1 = f"Supply of {desc}"
        if p and not l1.endswith(f"- {p}") and not l1.endswith(p):
            l1 = f"{l1} - {p}"
    elif p:
        l1 = f"Supply of - {p}" if not p.lower().startswith("supply of") else p
    else:
        l1 = ""

    if l1:
        lines.append(l1)

    s = (spec or "").strip()
    if s:
        lines.append(s)

    c = (commodity_code or "").strip()
    if c:
        if c.lower().startswith("commodity code:"):
            lines.append(c)
        else:
            lines.append(f"Commodity Code: {c}")

    return "\n".join(lines)


# ---------------------------------------------------------------- quotation lines
# The price sheet is the single source of truth. A description the user saved in "Quotation
# details" is stored with the sheet line it was written for ("src" = part|description) and is
# only used for that line - never for whatever line happens to sit at the same position later.

def _n(text):
    return " ".join(str(text or "").split()).lower()


def line_signature(ln):
    return f"{_n(ln.get('part'))}|{_n(ln.get('description'))}"


def detail_for_line(line_details, idx, ln):
    """The saved quotation details (description / commodity code / spec) for sheet line `ln`
    at position `idx`, or {} when nothing saved belongs to this line."""
    lds = line_details or {}
    sig = line_signature(ln)
    ld = lds.get(str(idx)) or lds.get(idx) or {}
    if isinstance(ld, dict) and ld.get("src") == sig:
        return ld
    for v in lds.values():
        if isinstance(v, dict) and v.get("src") == sig:
            return v
    if isinstance(ld, dict) and ld and not ld.get("src"):
        # saved before lines were tied to the sheet: use it only if it clearly is about this line
        text = _n(ld.get("quote_description") or ld.get("description") or "")
        part, desc = _n(ln.get("part")), _n(ln.get("description"))
        if not text:
            return ld                  # only a commodity code / spec typed for this line
        if (part and part in text) or (not part and desc and desc in text):
            return ld
    return {}


def clean_line_details(line_details, lines):
    """line_details re-keyed to the current sheet lines (1, 2, ...), each stamped with its line;
    saved texts that belong to no current line are dropped."""
    out = {}
    for i, ln in enumerate(lines, 1):
        ld = dict(detail_for_line(line_details, i, ln))
        ld["src"] = line_signature(ln)
        out[str(i)] = ld
    return out


def stamp_line_details(line_details, lines):
    """After the user edited quotation details: entries without a line get the line shown at
    that position, so the edit stays with that line even if rows move later."""
    out = {}
    for k, v in (line_details or {}).items():
        if not isinstance(v, dict):
            continue
        v = dict(v)
        if not v.get("src"):
            try:
                i = int(k)
            except (TypeError, ValueError):
                i = 0
            if 1 <= i <= len(lines):
                v["src"] = line_signature(lines[i - 1])
        out[str(k)] = v
    return out


def quotation_line(idx, ln, line_details):
    """Description text and details for one quotation line, exactly as it goes to Zoho."""
    ld = detail_for_line(line_details, idx, ln)
    spec = ld.get("notes") or ld.get("spec") or ""
    cc = ld.get("commodity_code") or ""
    text = build_line_description(
        description=ln.get("description", ""), part=ln.get("part", ""), spec=spec, commodity_code=cc,
        edited_description=ld.get("quote_description") or ld.get("description"),
    )
    return {"description": text, "commodity_code": cc, "spec": spec}


def build_line_items(lines, line_details):
    """Zoho line_items from the price sheet lines: description, qty, unit, rate (IDR)."""
    items = []
    for i, ln in enumerate(lines, 1):
        item = {
            "description": quotation_line(i, ln, line_details)["description"][:2000],
            "quantity": ln["qty"],
            "rate": ln["rate"],
        }
        if ln.get("unit"):
            item["unit"] = ln["unit"][:20]
        items.append(item)
    return items


def compare_line_items(sent, est):
    """Differences between the line items sent and what Zoho stored, e.g. ['line 2 price']."""
    if "line_items" not in (est or {}):
        return []                      # nothing to compare against
    got = est.get("line_items") or []
    if len(got) != len(sent):
        return [f"{len(got)} lines instead of {len(sent)}"]
    diffs = []
    for i, (a, b) in enumerate(zip(sent, got), 1):
        if abs(float(a["quantity"]) - float(b.get("quantity") or 0)) > 1e-6:
            diffs.append(f"line {i} qty")
        if abs(float(a["rate"]) - float(b.get("rate") or 0)) > 0.01:
            diffs.append(f"line {i} price")
        if _n(a["description"]) != _n(b.get("description")):
            diffs.append(f"line {i} description")
    return diffs


def update_draft_estimate(estimate_id, *, lines, line_details=None, quote_subject=""):
    """Rewrite the lines (and subject) of an existing Zoho DRAFT quotation from the price sheet."""
    data = api("GET", f"estimates/{estimate_id}")
    est = data.get("estimate") or {}
    if not est:
        raise ZohoError("The quotation was not found in Zoho.")
    status = str(est.get("status") or "").lower()
    if status and status != "draft":
        raise ZohoError(f"Zoho quotation {est.get('estimate_number', '')} is '{status}', not a draft - "
                        f"it can't be changed from here. Make a revision instead.")
    items = build_line_items(lines, line_details)
    payload = {"customer_id": est.get("customer_id"), "line_items": items}
    if quote_subject and str(quote_subject).strip():
        payload["subject_content"] = str(quote_subject).strip()[:255]
    api("PUT", f"estimates/{estimate_id}", None, payload)
    check = api("GET", f"estimates/{estimate_id}").get("estimate") or {}
    return compare_line_items(items, check)


def get_primary_contact_person_id(customer_id):
    """Get the primary contact person ID for a customer in Zoho Books."""
    if not customer_id or not is_configured():
        return None
    try:
        conn = ZohoConnection.get()
        if not conn.is_connected:
            return None
        data = api("GET", f"contacts/{customer_id}")
        contact = data.get("contact") or {}
        contact_persons = contact.get("contact_persons") or []
        for cp in contact_persons:
            if cp.get("is_primary_contact"):
                return cp.get("contact_person_id")
        if contact_persons:
            return contact_persons[0].get("contact_person_id")
    except Exception as e:
        logger.warning("Could not fetch primary contact person for customer %s: %s", customer_id, e)
    return None


def get_customer_default_salesperson(customer_id):
    """Get the salesperson from the customer's most recent Zoho estimate."""
    if not customer_id or not is_configured():
        return "", ""
    try:
        conn = ZohoConnection.get()
        if not conn.is_connected:
            return "", ""
        data = api("GET", "estimates", {
            "customer_id": customer_id,
            "sort_column": "created_time",
            "sort_order": "D",
            "per_page": 1,
        })
        estimates = data.get("estimates") or []
        if estimates:
            est = estimates[0]
            return (est.get("salesperson_id") or "").strip(), (est.get("salesperson_name") or "").strip()
    except Exception as e:
        logger.warning("Could not fetch customer default salesperson for %s: %s", customer_id, e)
    return "", ""


def get_salespersons_list():
    """Distinct salespersons from the last ~200 estimates in Zoho Books, cached for 1 day."""
    from django.core.cache import cache
    from ..company import current
    cache_key = f"zoho_distinct_salespersons_{current()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    if not is_configured():
        return []
    try:
        conn = ZohoConnection.get()
        if not conn.is_connected:
            return []
        data = api("GET", "estimates", {"per_page": 200, "sort_column": "created_time", "sort_order": "D"})
        estimates = data.get("estimates") or []
        seen = {}
        for est in estimates:
            sp_name = (est.get("salesperson_name") or "").strip()
            sp_id = (est.get("salesperson_id") or "").strip()
            if sp_name and sp_name not in seen:
                seen[sp_name] = {"id": sp_id, "name": sp_name}
        result = sorted(seen.values(), key=lambda x: x["name"].lower())
        cache.set(cache_key, result, 86400)
        return result
    except Exception as e:
        logger.warning("Could not fetch salespersons from Zoho: %s", e)
        return []


def create_draft_estimate(
    *,
    customer_id,
    reference,
    lines,
    estimate_number=None,
    notes="",
    quote_subject="",
    validity_days=30,
    salesperson_name="",
    salesperson_id="",
    delivery_time="",
    payment_terms="",
    customer_notes="",
    extra_terms="",
    line_details=None,
    date=None,
):
    quote_date = date or timezone.localdate()
    val_days = int(validity_days or 30)
    expiry = quote_date + timedelta(days=val_days)

    line_items = build_line_items(lines, line_details)

    payload = {
        "customer_id": customer_id,
        "reference_number": reference[:50],
        "date": quote_date.isoformat(),
        "expiry_date": expiry.isoformat(),
        "line_items": line_items,
    }

    if quote_subject and str(quote_subject).strip():
        payload["subject_content"] = str(quote_subject).strip()[:255]

    sp_id = str(salesperson_id or "").strip()
    sp_name = str(salesperson_name or "").strip()
    if sp_id:
        payload["salesperson_id"] = sp_id
    elif sp_name:
        payload["salesperson_name"] = sp_name

    cust_notes = str(customer_notes or notes or "").strip()
    if cust_notes:
        payload["notes"] = cust_notes[:2000]

    # Build terms
    term_lines = []
    if delivery_time and str(delivery_time).strip():
        term_lines.append(f"Delivery Time : {str(delivery_time).strip()}")
    if payment_terms and str(payment_terms).strip():
        term_lines.append(f"Payment Terms : {str(payment_terms).strip()}")
    if val_days:
        term_lines.append(f"Validity : {val_days} days")
    if extra_terms and str(extra_terms).strip():
        term_lines.append(str(extra_terms).strip())

    if term_lines:
        payload["terms"] = "\n".join(term_lines)[:5000]

    # Primary contact person
    try:
        primary_cp_id = get_primary_contact_person_id(customer_id)
        if primary_cp_id:
            payload["contact_persons"] = [primary_cp_id]
    except Exception as cp_err:
        logger.warning("Could not set primary contact person: %s", cp_err)

    params = {}
    if estimate_number:
        payload["estimate_number"] = estimate_number
        params["ignore_auto_number_generation"] = "true"

    data = api("POST", "estimates", params, payload)
    est = data.get("estimate") or {}
    if not est.get("estimate_id"):
        raise ZohoError("Zoho did not return the new quotation.")

    estimate_id = est["estimate_id"]
    est_number = est.get("estimate_number", "")

    # Verification read-back: check if subject_content or salesperson stuck
    stick_warnings = []
    try:
        verify_data = api("GET", f"estimates/{estimate_id}")
        fetched_est = verify_data.get("estimate") or {}
        if payload.get("subject_content") and not fetched_est.get("subject_content"):
            stick_warnings.append("Subject")
        if (payload.get("salesperson_id") or payload.get("salesperson_name")) and not (
            fetched_est.get("salesperson_id") or fetched_est.get("salesperson_name")
        ):
            stick_warnings.append("Salesperson")
        line_diffs = compare_line_items(line_items, fetched_est)
        if line_diffs:
            stick_warnings.append("the line items (" + ", ".join(line_diffs) + ")")
    except Exception as verify_err:
        logger.warning("Could not verify created estimate %s in Zoho: %s", estimate_id, verify_err)

    return EstimateResult(estimate_id, est_number, stick_warnings)


def estimate_url(estimate_id, company=None):
    return f"https://books.zoho.com/app/{org_id(company)}#/quotes/{estimate_id}"


def next_manual_number(year=None):
    """Next quotation number for a company without Zoho auto numbering, e.g. FFE_26000082.
    Highest FFE_<yy>nnnnnn in Zoho (revisions R1.. ignored) + 1; a new year starts at FFE_<yy>000001."""
    import re
    from ..company import current
    prefix = MANUAL_NUMBER_PREFIX.get(current())
    if not prefix:
        return None
    yy = (year or timezone.localdate().year) % 100
    start = f"{prefix}_{yy:02d}"
    pat = re.compile(rf"^{re.escape(start)}(\d+)(?:R\d+)?$", re.I)
    best, width, page = 0, 6, 1
    while page <= 10:
        data = api("GET", "estimates", {"estimate_number_startswith": start, "per_page": 200, "page": page})
        for est in data.get("estimates") or []:
            m = pat.match((est.get("estimate_number") or "").strip())
            if m:
                best = max(best, int(m.group(1)))
                width = max(width, len(m.group(1)))
        if not (data.get("page_context") or {}).get("has_more_page"):
            break
        page += 1
    return f"{start}{best + 1:0{width}d}"
