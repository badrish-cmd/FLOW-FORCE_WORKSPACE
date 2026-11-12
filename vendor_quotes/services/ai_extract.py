"""
AI reading and extraction service for vendor quotes using Google Gemini.

Supports PDF, Excel (.xlsx, .xls), and image formats. Extracts line items,
pricing factors, and quote metadata using Gemini structured outputs.
"""
import io
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Maximum file size allowed for AI extraction (20 MB)
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024


class AIExtractError(Exception):
    """Raised when vendor quote extraction fails."""
    pass


class LineItemSchema(BaseModel):
    line_no: Optional[int] = Field(None, description="Original item line number from the quote")
    part_no: Optional[str] = Field(None, description="Part number, product code, or item identifier")
    description: Optional[str] = Field(None, description="Item description or name")
    qty: Optional[float] = Field(None, description="Item quantity (positive number)")
    unit: Optional[str] = Field("pcs", description="Unit of measure (e.g., pcs, set, m, kg)")
    unit_price: Optional[float] = Field(None, description="Price per single unit BEFORE any discount")
    discount_percent: Optional[float] = Field(None, description="Discount percentage between 0 and 100, if any")
    notes: Optional[str] = Field(None, description="Additional notes, specifications, model, or make for this item")
    commodity_code: Optional[str] = Field(None, description="HS code, customs tariff number, or commodity code if printed")
    packing_amount: Optional[float] = Field(None, description="Packing / packaging charge printed for THIS item, in vendor currency")
    packing_basis: Optional[str] = Field(None, description="'per_unit' if packing_amount is for each unit, 'per_line' if it is for the whole line quantity")


class QuoteExtractSchema(BaseModel):
    vendor_quote_no: Optional[str] = Field(None, description="Vendor's quotation or offer number")
    quote_date: Optional[str] = Field(None, description="Date of quote in YYYY-MM-DD or original format")
    currency: Optional[str] = Field(None, description="3-letter ISO code of the prices, e.g. EUR, USD, INR, IDR. 'Rs.'/'₹' = INR, '$'/'US$' = USD")
    freight_or_courier: Optional[float] = Field(None, description="One lump-sum freight/courier/transport amount in vendor currency (not per-unit packing)")
    quote_total: Optional[float] = Field(None, description="Total quotation amount in vendor currency")
    packing_total: Optional[float] = Field(None, description="One packing amount for the whole quote when it is NOT printed per item, in vendor currency")
    quote_subject: Optional[str] = Field(None, description="Short product summary, max ~80 characters, e.g. 'Airlock DMN Westinghouse AML300 4TCO'")
    delivery_time: Optional[str] = Field(None, description="Delivery or lead time stated in quote, formatted e.g. '10 to 12 weeks from PO'")
    lines: List[LineItemSchema] = Field(default_factory=list, description="Extracted line items")
    warnings: List[str] = Field(default_factory=list, description="Warnings or issues detected (e.g. skipped unpriced items)")


EXTRACTION_PROMPT = """You are an expert procurement and quoting specialist.
Extract all line items and quotation details from the attached vendor quote file.

Strict extraction rules:
1. Copy numbers exactly as printed in the document. Never invent or guess values.
2. Decimal commas vs periods: Note European formatting where periods are thousands separators and commas are decimal points (e.g., '1.234,56' means 1234.56). Convert European comma decimals to standard decimals.
   Indian formatting groups digits in lakhs: 'Rs. 2,46,000/-' means 246000 and 'INR 6,08,500/-' means 608500. Drop the trailing '/-'.
3. Unit Price: 'unit_price' MUST be the price per single unit BEFORE discount.
4. Discount: If the quote indicates a discount percentage on a line item, set 'discount_percent' (0 to 100, e.g. 15 for 15%). If no discount is shown, use null.
5. Packing is NEVER a line in 'lines':
   - Packing printed for an item goes on that item: 'packing_amount' + 'packing_basis'.
     'Packing Charges: Extra @9800/- each' for an item of 4 nos -> packing_amount 9800, packing_basis 'per_unit'.
     'Packaging Costs EUR 500.00' printed under a line of 4 nos -> packing_amount 500, packing_basis 'per_line'.
   - One packing amount for the whole quote (not tied to an item) -> 'packing_total'.
   - Freight, courier or transport with an amount -> 'freight_or_courier' (information only). 'Freight extra at
     actuals' / 'client scope' or any charge without an amount: leave it out and add a warning.
5b. Priced accessories and options quoted 'each' under an item (e.g. 'Venturi: INR 50,000/- each', 'ZSS: INR 53,000/- each')
   are separate lines with the qty of the item they belong to. Quantities written like '04 x 350mm' or 'Number of units: 03' mean qty 4 / 3.
6. Price on request: Skip any line items where the price is 'price on request', 'on request', 'price will follow', 'TBA', or missing, and add an explanatory warning string to 'warnings' for each skipped item.
7. Currency: Detect the currency the PRICES are written in and return its 3-letter ISO code
   (EUR, USD, INR, IDR, SGD, GBP, JPY ...). 'Rs.', 'Rs', '₹' or 'Rupees' = INR; '$', 'US$', 'USD' = USD; '€' = EUR.
   Look at the price lines themselves, not the company address or bank details.
8. Quote Subject: Extract a concise product summary (max ~80 characters) summarizing the quoted equipment or goods (e.g. 'Airlock DMN Westinghouse AML300 4TCO').
9. Delivery Time: If the vendor quote states a delivery time or lead time, extract it as 'X to Y weeks from PO' (or the printed lead time).
10. Commodity Code: If an HS code, tariff code, or commodity code is printed for an item, extract it into 'commodity_code'.
11. Never invent values: If a field is not present or unknown, return null.
"""


def _get_api_key() -> str:
    key = getattr(settings, "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    return key.strip()


def _get_model_name() -> str:
    model = getattr(settings, "GEMINI_MODEL", "") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    return model.strip() or "gemini-2.5-flash"


def excel_to_text(file_bytes: bytes, filename: str) -> str:
    """Convert an Excel workbook (.xlsx/.xls) to plain text with sheet names and tab-separated rows."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as e:
        raise AIExtractError(f"Could not read Excel file '{filename}': {e}")

    parts = []
    for sheetname in wb.sheetnames:
        sheet = wb[sheetname]
        parts.append(f"--- Sheet: {sheetname} ---")
        for row in sheet.iter_rows(values_only=True):
            if any(cell is not None for cell in row):
                row_str = "\t".join("" if cell is None else str(cell).strip() for cell in row)
                parts.append(row_str)

    content = "\n".join(parts).strip()
    if not content:
        raise AIExtractError(f"Excel file '{filename}' contains no readable data.")
    return content


def validate_and_clean_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate Gemini extraction result in Python:
    - Qty > 0
    - Unit price >= 0
    - Discount between 0 and 100
    - Drop invalid lines into warnings
    """
    warnings = list(raw_data.get("warnings") or [])
    cleaned_lines = []
    raw_lines = raw_data.get("lines") or []

    for idx, item in enumerate(raw_lines, 1):
        if not isinstance(item, dict):
            if hasattr(item, "model_dump"):
                item = item.model_dump()
            elif hasattr(item, "__dict__"):
                item = item.__dict__
            else:
                continue

        part_no = (item.get("part_no") or "").strip()
        desc = (item.get("description") or "").strip()
        line_ident = part_no or desc or f"Line #{idx}"

        if not part_no and not desc:
            warnings.append(f"Line #{idx} dropped: both part number and description are empty.")
            continue

        qty = item.get("qty")
        if qty is None or not isinstance(qty, (int, float)) or qty <= 0:
            warnings.append(f"Line '{line_ident}' dropped: invalid quantity ({qty}). Quantity must be > 0.")
            continue

        unit_price = item.get("unit_price")
        if unit_price is None or not isinstance(unit_price, (int, float)) or unit_price < 0:
            warnings.append(f"Line '{line_ident}' dropped: invalid unit price ({unit_price}). Price must be >= 0.")
            continue

        disc = item.get("discount_percent")
        if disc is not None:
            if not isinstance(disc, (int, float)) or disc < 0 or disc > 100:
                warnings.append(f"Line '{line_ident}': discount {disc}% is out of bounds (0-100); ignored discount.")
                disc = None
            else:
                disc = float(disc)

        packing_per_unit = 0.0
        pk = item.get("packing_amount")
        if isinstance(pk, (int, float)) and not isinstance(pk, bool) and pk > 0:
            basis = str(item.get("packing_basis") or "per_unit").strip().lower()
            packing_per_unit = round(float(pk) / float(qty), 4) if basis == "per_line" else float(pk)

        cleaned_lines.append({
            "line_no": item.get("line_no") or len(cleaned_lines) + 1,
            "part_no": part_no,
            "description": desc,
            "qty": float(qty) if isinstance(qty, (int, float)) else qty,
            "unit": (item.get("unit") or "pcs").strip() or "pcs",
            "unit_price": float(unit_price) if isinstance(unit_price, (int, float)) else unit_price,
            "discount_percent": disc,
            "notes": (item.get("notes") or "").strip(),
            "commodity_code": (item.get("commodity_code") or "").strip() or None,
            "packing_per_unit": packing_per_unit,
        })

    from .sheet import normalize_currency
    currency = normalize_currency(raw_data.get("currency")) or None

    freight = raw_data.get("freight_or_courier")
    if freight is not None:
        try:
            freight = float(freight)
            if freight < 0:
                freight = None
        except (ValueError, TypeError):
            freight = None

    quote_total = raw_data.get("quote_total")
    if quote_total is not None:
        try:
            quote_total = float(quote_total)
            if quote_total < 0:
                quote_total = None
        except (ValueError, TypeError):
            quote_total = None

    packing_total = raw_data.get("packing_total")
    try:
        packing_total = float(packing_total) if packing_total is not None else None
        if packing_total is not None and packing_total <= 0:
            packing_total = None
    except (ValueError, TypeError):
        packing_total = None

    subject = (raw_data.get("quote_subject") or "").strip()
    if subject:
        subject = subject[:255]
    else:
        subject = None

    deliv = (raw_data.get("delivery_time") or "").strip()
    if deliv:
        deliv = deliv[:255]
    else:
        deliv = None

    return {
        "vendor_quote_no": (raw_data.get("vendor_quote_no") or "").strip() or None,
        "quote_date": (raw_data.get("quote_date") or "").strip() or None,
        "currency": currency or None,
        "freight_or_courier": freight,
        "quote_total": quote_total,
        "packing_total": packing_total,
        "quote_subject": subject,
        "delivery_time": deliv,
        "lines": cleaned_lines,
        "warnings": warnings,
    }


def _safe_int(val, default=0):
    try:
        if isinstance(val, (int, float, str)) and not isinstance(val, bool):
            return int(val)
    except (ValueError, TypeError):
        pass
    return default


def extract_quote(file_bytes: bytes, filename: str, user=None, quote=None) -> Dict[str, Any]:
    """
    Extract structured quotation data from a vendor file using Google Gemini.
    Every call attempt is logged to AIRequestLog (success or failure) with tokens and cost.

    Args:
        file_bytes: Raw binary bytes of the uploaded file.
        filename: Name of the file (e.g. quote.pdf, quote.xlsx, quote.jpg).
        user: Optional User initiating the extraction.
        quote: Optional VendorQuote instance.

    Returns:
        dict: Validated quote data.

    Raises:
        AIExtractError: If the key is missing, file is unreadable, or extraction fails.
    """
    t0 = time.time()
    file_size = len(file_bytes) if file_bytes else 0
    ext = os.path.splitext(filename.lower())[1] if filename else ""
    if ext in (".xlsx", ".xls"):
        file_type = "xlsx"
    elif ext == ".pdf":
        file_type = "pdf"
    elif ext in (".png", ".jpg", ".jpeg", ".webp", ".heic", ".tiff", ".tif"):
        file_type = "image"
    else:
        file_type = ext.lstrip(".") or "unknown"

    model_name = _get_model_name()
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    lines_extracted = 0
    warnings_count = 0
    status = "SUCCESS"
    error_msg = ""

    try:
        if not file_bytes:
            raise AIExtractError("The uploaded file is empty.")

        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            raise AIExtractError(
                f"File size ({len(file_bytes) / (1024 * 1024):.1f} MB) exceeds the 20 MB limit."
            )

        api_key = _get_api_key()
        if not api_key:
            raise AIExtractError("Gemini API key is not configured. Set GEMINI_API_KEY in environment.")

        is_excel = ext in (".xlsx", ".xls")
        is_pdf = ext == ".pdf"
        is_image = ext in (".png", ".jpg", ".jpeg", ".webp", ".heic", ".tiff", ".tif")

        if not (is_excel or is_pdf or is_image):
            raise AIExtractError(
                f"Unsupported file format '{ext}'. Please upload a PDF, Excel spreadsheet (.xlsx/.xls), or image (PNG, JPEG, WEBP)."
            )

        # Initialize Gemini client
        try:
            from google import genai
            from google.genai import types
            # Structured JSON output (response_schema) is only available on the v1beta API;
            # google-genai 3.x defaults to v1, which rejects it with "JSON mode is not enabled".
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        except Exception as e:
            raise AIExtractError(f"Failed to initialize Gemini client: {e}")

        # Prepare contents
        contents = []
        if is_excel:
            sheet_text = excel_to_text(file_bytes, filename)
            contents.append(
                f"Here is the text extracted from the vendor quote Excel file '{filename}':\n\n{sheet_text}\n\n{EXTRACTION_PROMPT}"
            )
        else:
            mime_map = {
                ".pdf": "application/pdf",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".heic": "image/heic",
                ".tiff": "image/tiff",
                ".tif": "image/tiff",
            }
            mime_type = mime_map.get(ext, "application/pdf")
            try:
                file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
            except Exception as e:
                raise AIExtractError(f"Failed to prepare file for Gemini: {e}")

            contents.append(file_part)
            contents.append(EXTRACTION_PROMPT)

        # Call Gemini model with structured output
        try:
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=QuoteExtractSchema,
                temperature=0.0,
            )
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
        except Exception as e:
            logger.exception("Gemini API call failed for file %s", filename)
            raise AIExtractError(f"Gemini API request failed: {e}")

        # Read usage_metadata (tokens)
        usage = getattr(response, "usage_metadata", None)
        if usage:
            if isinstance(usage, dict):
                input_tokens = _safe_int(usage.get("prompt_token_count"), 0)
                output_tokens = _safe_int(usage.get("candidates_token_count"), 0)
                total_tokens = _safe_int(usage.get("total_token_count"), 0)
            else:
                input_tokens = _safe_int(getattr(usage, "prompt_token_count", None), 0)
                output_tokens = _safe_int(getattr(usage, "candidates_token_count", None), 0)
                total_tokens = _safe_int(getattr(usage, "total_token_count", None), 0)
        if total_tokens == 0 and (input_tokens + output_tokens) > 0:
            total_tokens = input_tokens + output_tokens

        # Parse response
        raw_data = None
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, BaseModel):
            raw_data = parsed.model_dump()
        elif isinstance(parsed, dict):
            raw_data = parsed

        if raw_data is None:
            response_text = getattr(response, "text", "") or ""
            try:
                raw_data = json.loads(response_text)
            except Exception as e:
                logger.error("Gemini returned invalid JSON: %s", response_text[:500])
                raise AIExtractError(f"Gemini returned invalid JSON: {e}")

        if not isinstance(raw_data, dict):
            raise AIExtractError("Gemini returned an unexpected response structure.")

        # Validate and clean data
        result = validate_and_clean_data(raw_data)
        lines_extracted = len(result.get("lines") or [])
        warnings_count = len(result.get("warnings") or [])
        return result

    except Exception as e:
        status = "FAILED"
        error_msg = str(e)
        raise
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        try:
            _record_request_log(
                user=user,
                quote=quote,
                model_name=model_name,
                filename=filename,
                file_type=file_type,
                file_size=file_size,
                status=status,
                error_msg=error_msg,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                lines_extracted=lines_extracted,
                warnings_count=warnings_count,
            )
        except Exception as log_exc:
            logger.warning("Could not record AIRequestLog: %s", log_exc)


def _record_request_log(
    user, quote, model_name, filename, file_type, file_size, status,
    error_msg, duration_ms, input_tokens, output_tokens, total_tokens,
    lines_extracted, warnings_count
):
    from django.utils import timezone
    from ..models import AIRequestLog

    input_tokens = _safe_int(input_tokens, 0)
    output_tokens = _safe_int(output_tokens, 0)
    total_tokens = _safe_int(total_tokens, 0)
    lines_extracted = _safe_int(lines_extracted, 0)
    warnings_count = _safe_int(warnings_count, 0)
    duration_ms = _safe_int(duration_ms, 0)
    file_size = _safe_int(file_size, 0)

    price_in = getattr(settings, "GEMINI_PRICE_INPUT_PER_M", Decimal("0"))
    if not isinstance(price_in, Decimal):
        price_in = Decimal(str(price_in))
    price_out = getattr(settings, "GEMINI_PRICE_OUTPUT_PER_M", Decimal("0"))
    if not isinstance(price_out, Decimal):
        price_out = Decimal(str(price_out))

    cost = (Decimal(input_tokens) / Decimal(1000000)) * price_in + (Decimal(output_tokens) / Decimal(1000000)) * price_out

    log_entry = AIRequestLog.objects.create(
        user=user if (user and getattr(user, "is_authenticated", False)) else None,
        quote=quote,
        feature="quote_reading",
        provider="gemini",
        model=model_name,
        file_name=filename or "",
        file_type=file_type or "",
        file_size_bytes=file_size,
        status=status,
        error_message=error_msg,
        duration_ms=duration_ms,
        finished_at=timezone.now(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        lines_extracted=lines_extracted,
        warnings_count=warnings_count,
        estimated_cost_usd=cost,
    )

    if status == AIRequestLog.STATUS_SUCCESS:
        try:
            from . import workflow
            workflow.check_ai_budget_alerts(quote=quote)
        except Exception as e:
            logger.warning("Could not run check_ai_budget_alerts: %s", e)

    return log_entry
