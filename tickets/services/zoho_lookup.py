"""
Zoho Books read-only lookup service for Tickets & Projects.

Links Flow-Force Tickets to Zoho Books records using:
  - JOB No. (e.g. "1921") found in Sales Order numbers/terms/subjects
  - Enquiry No. (e.g. "ENQ/2026/09/2708") found in Estimates reference numbers

Strictly READ-ONLY. Never modifies or writes to Zoho Books.
"""
import logging
import re
from django.core.cache import cache
from vendor_quotes.services import zoho

logger = logging.getLogger(__name__)

CACHE_TTL = 600  # 10 minutes

# Exact regex to match JOB No: e.g. "JOB No. 1921" or "JOB No 1921" or "JOB# 1921"
JOB_REGEX = re.compile(r"JOB\s*No\.?\s*(\d+)", re.IGNORECASE)


def _format_address(addr):
    """Format address dict into a multi-line string."""
    if not isinstance(addr, dict):
        return ""
    parts = [
        addr.get("address", "").strip(),
        addr.get("street2", "").strip(),
        addr.get("city", "").strip(),
        addr.get("state", "").strip(),
        addr.get("zip", "").strip(),
        addr.get("country", "").strip(),
    ]
    return ", ".join(p for p in parts if p)


def _get_contact_details(customer_id):
    """Fetch customer primary contact person, phone, email, and site address."""
    details = {
        "client_contact_name": "",
        "client_phone": "",
        "client_email": "",
        "site_address": "",
    }
    if not customer_id:
        return details

    try:
        data = zoho.api("GET", f"contacts/{customer_id}")
        contact = data.get("contact", {})

        # Primary contact person
        persons = contact.get("contact_persons", [])
        primary = next((p for p in persons if p.get("is_primary_contact")), None)
        if not primary and persons:
            primary = persons[0]

        if primary:
            fname = primary.get("first_name", "").strip()
            lname = primary.get("last_name", "").strip()
            details["client_contact_name"] = f"{fname} {lname}".strip() or primary.get("email", "")
            details["client_phone"] = primary.get("phone") or primary.get("mobile") or ""
            details["client_email"] = primary.get("email", "")
        else:
            details["client_phone"] = contact.get("phone") or contact.get("mobile") or ""
            details["client_email"] = contact.get("email", "")

        # Prefer shipping address for site address, fallback to billing
        shipping = contact.get("shipping_address") or {}
        billing = contact.get("billing_address") or {}
        details["site_address"] = _format_address(shipping) or _format_address(billing)

    except Exception as e:
        logger.warning("Could not fetch Zoho contact details for %s: %s", customer_id, e)

    return details


def lookup_by_job_no(job_no):
    """
    Search Zoho Books for records matching JOB No.
    Scans Sales Orders for 'JOB No. {job_no}' in salesorder_number, reference_number, subject, notes.
    """
    if not job_no:
        return None

    clean_job = str(job_no).strip()
    cache_key = f"zoho_lookup_job_{clean_job}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result = {
        "found": False,
        "job_no": clean_job,
        "enquiry_number": "",
        "quote_number": "",
        "sales_order_numbers": [],
        "customer_name": "",
        "zoho_customer_id": "",
        "salesperson_name": "",
        "client_contact_name": "",
        "client_phone": "",
        "client_email": "",
        "site_address": "",
        "source": "zoho",
    }

    if not zoho.is_configured():
        logger.info("Zoho is not configured. Skipping lookup for JOB %s", clean_job)
        return result

    try:
        # Fetch recent sales orders from Zoho Books
        so_data = zoho.api("GET", "salesorders", {"per_page": 200, "sort_column": "date", "sort_order": "D"})
        sales_orders = so_data.get("salesorders", [])

        matching_sos = []
        for so in sales_orders:
            searchable_text = " ".join([
                so.get("salesorder_number", ""),
                so.get("reference_number", ""),
                so.get("subject", "") or "",
                so.get("customer_name", ""),
            ])
            matches = JOB_REGEX.findall(searchable_text)
            # Exact match on integer to avoid "1921" matching "19210"
            if any(m == clean_job for m in matches):
                matching_sos.append(so)

        if not matching_sos:
            cache.set(cache_key, result, timeout=CACHE_TTL)
            return result

        result["found"] = True
        result["sales_order_numbers"] = [so.get("salesorder_number") for so in matching_sos if so.get("salesorder_number")]

        # Use primary sales order for metadata
        primary_so = matching_sos[0]
        result["customer_name"] = primary_so.get("customer_name", "")
        result["zoho_customer_id"] = primary_so.get("customer_id", "")
        result["salesperson_name"] = primary_so.get("salesperson_name", "")
        result["quote_number"] = primary_so.get("reference_number", "")

        # Lookup Enquiry No. from the quote in Zoho Estimates if quote_number is present
        if result["quote_number"]:
            try:
                est_data = zoho.api("GET", "estimates", {"estimate_number": result["quote_number"]})
                estimates = est_data.get("estimates", [])
                if estimates:
                    result["enquiry_number"] = estimates[0].get("reference_number", "")
            except Exception as e:
                logger.debug("Could not resolve estimate reference for quote %s: %s", result["quote_number"], e)

        # Contact and site details
        if result["zoho_customer_id"]:
            contact_info = _get_contact_details(result["zoho_customer_id"])
            result.update(contact_info)

        cache.set(cache_key, result, timeout=CACHE_TTL)
        return result

    except Exception as e:
        logger.warning("Zoho lookup failed for JOB %s: %s", clean_job, e)
        result["error"] = str(e)
        return result


def lookup_by_enquiry(enquiry_number):
    """
    Search Zoho Books for records matching Enquiry Number (e.g. ENQ/2026/09/2708).
    Searches Estimates reference numbers, resolves Quote No. and associated Sales Orders for JOB No.
    """
    if not enquiry_number:
        return None

    clean_enq = str(enquiry_number).strip()
    cache_key = f"zoho_lookup_enq_{clean_enq}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result = {
        "found": False,
        "job_no": "",
        "enquiry_number": clean_enq,
        "quote_number": "",
        "sales_order_numbers": [],
        "customer_name": "",
        "zoho_customer_id": "",
        "salesperson_name": "",
        "client_contact_name": "",
        "client_phone": "",
        "client_email": "",
        "site_address": "",
        "source": "zoho",
    }

    if not zoho.is_configured():
        logger.info("Zoho is not configured. Skipping lookup for ENQ %s", clean_enq)
        return result

    try:
        est_data = zoho.api("GET", "estimates", {"reference_number_contains": clean_enq, "per_page": 50})
        estimates = est_data.get("estimates", [])

        # Filter exact or close matches
        matching_ests = [e for e in estimates if clean_enq.lower() in (e.get("reference_number") or "").lower()]
        if not matching_ests:
            cache.set(cache_key, result, timeout=CACHE_TTL)
            return result

        result["found"] = True
        primary_est = matching_ests[0]
        result["quote_number"] = primary_est.get("estimate_number", "")
        result["customer_name"] = primary_est.get("customer_name", "")
        result["zoho_customer_id"] = primary_est.get("customer_id", "")
        result["salesperson_name"] = primary_est.get("salesperson_name", "")

        # Try to find associated sales order using the estimate/quote number
        if result["quote_number"]:
            try:
                so_data = zoho.api("GET", "salesorders", {"reference_number_contains": result["quote_number"], "per_page": 50})
                sos = so_data.get("salesorders", [])
                matching_sos = [so for so in sos if result["quote_number"].lower() in (so.get("reference_number") or "").lower()]
                if matching_sos:
                    result["sales_order_numbers"] = [so.get("salesorder_number") for so in matching_sos if so.get("salesorder_number")]
                    # Check for JOB No in the SO text
                    for so in matching_sos:
                        searchable = f"{so.get('salesorder_number', '')} {so.get('reference_number', '')} {so.get('subject', '')}"
                        m = JOB_REGEX.search(searchable)
                        if m:
                            result["job_no"] = m.group(1)
                            break
            except Exception as e:
                logger.debug("Could not resolve sales orders for quote %s: %s", result["quote_number"], e)

        # Contact and site details
        if result["zoho_customer_id"]:
            contact_info = _get_contact_details(result["zoho_customer_id"])
            result.update(contact_info)

        cache.set(cache_key, result, timeout=CACHE_TTL)
        return result

    except Exception as e:
        logger.warning("Zoho lookup failed for ENQ %s: %s", clean_enq, e)
        result["error"] = str(e)
        return result

