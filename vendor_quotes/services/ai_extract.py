"""
AI reading and extraction service for vendor quotes using Google Gemini.

Supports PDF, Excel (.xlsx, .xls), and image formats. Extracts line items,
pricing factors, and quote metadata using Gemini structured outputs.
"""
import io
import json
import logging
import os
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
    notes: Optional[str] = Field(None, description="Additional notes or specifications for this item")


class QuoteExtractSchema(BaseModel):
    vendor_quote_no: Optional[str] = Field(None, description="Vendor's quotation or offer number")
    quote_date: Optional[str] = Field(None, description="Date of quote in YYYY-MM-DD or original format")
    currency: Optional[str] = Field(None, description="3-letter currency ISO code, e.g. EUR, USD, IDR")
    freight_or_courier: Optional[float] = Field(None, description="Freight, transport, courier, or packing charge in vendor currency")
    quote_total: Optional[float] = Field(None, description="Total quotation amount in vendor currency")
    lines: List[LineItemSchema] = Field(default_factory=list, description="Extracted line items")
    warnings: List[str] = Field(default_factory=list, description="Warnings or issues detected (e.g. skipped unpriced items)")


EXTRACTION_PROMPT = """You are an expert procurement and quoting specialist.
Extract all line items and quotation details from the attached vendor quote file.

Strict extraction rules:
1. Copy numbers exactly as printed in the document. Never invent or guess values.
2. Decimal commas vs periods: Note European formatting where periods are thousands separators and commas are decimal points (e.g., '1.234,56' means 1234.56). Convert European comma decimals to standard decimals.
3. Unit Price: 'unit_price' MUST be the price per single unit BEFORE discount.
4. Discount: If the quote indicates a discount percentage on a line item, set 'discount_percent' (0 to 100, e.g. 15 for 15%). If no discount is shown, use null.
5. Freight / Packing / Courier: Put freight, packing, courier, handling, or transport charges into 'freight_or_courier', NOT as a line item in 'lines'.
6. Price on request: Skip any line items where the price is 'price on request', 'on request', 'price will follow', 'TBA', or missing, and add an explanatory warning string to 'warnings' for each skipped item.
7. Currency: Extract the currency as a 3-letter ISO code (e.g. EUR, USD, IDR, SGD, GBP, JPY).
8. Never invent values: If a field is not present or unknown, return null.
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

        cleaned_lines.append({
            "line_no": item.get("line_no") or len(cleaned_lines) + 1,
            "part_no": part_no,
            "description": desc,
            "qty": float(qty) if isinstance(qty, (int, float)) else qty,
            "unit": (item.get("unit") or "pcs").strip() or "pcs",
            "unit_price": float(unit_price) if isinstance(unit_price, (int, float)) else unit_price,
            "discount_percent": disc,
            "notes": (item.get("notes") or "").strip(),
        })

    currency = raw_data.get("currency")
    if currency:
        currency = str(currency).strip().upper()[:10]

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

    return {
        "vendor_quote_no": (raw_data.get("vendor_quote_no") or "").strip() or None,
        "quote_date": (raw_data.get("quote_date") or "").strip() or None,
        "currency": currency or None,
        "freight_or_courier": freight,
        "quote_total": quote_total,
        "lines": cleaned_lines,
        "warnings": warnings,
    }


def extract_quote(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """
    Extract structured quotation data from a vendor file using Google Gemini.

    Args:
        file_bytes: Raw binary bytes of the uploaded file.
        filename: Name of the file (e.g. quote.pdf, quote.xlsx, quote.jpg).

    Returns:
        dict: {
            "vendor_quote_no": str | None,
            "quote_date": str | None,
            "currency": str | None,
            "freight_or_courier": float | None,
            "quote_total": float | None,
            "lines": list of line dicts,
            "warnings": list of warning strings,
        }

    Raises:
        AIExtractError: If the key is missing, file is unreadable, or extraction fails.
    """
    if not file_bytes:
        raise AIExtractError("The uploaded file is empty.")

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise AIExtractError(
            f"File size ({len(file_bytes) / (1024 * 1024):.1f} MB) exceeds the 20 MB limit."
        )

    api_key = _get_api_key()
    if not api_key:
        raise AIExtractError("Gemini API key is not configured. Set GEMINI_API_KEY in environment.")

    model_name = _get_model_name()

    ext = os.path.splitext(filename.lower())[1]
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
    return validate_and_clean_data(raw_data)
