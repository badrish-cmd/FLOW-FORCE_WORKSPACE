"""
Helpers for Univer workbook snapshots (the JSON the Excel-like editor loads/saves).

Snapshot shape (subset of Univer's IWorkbookData):
{
  "id": "...", "name": "...", "styles": {style_id: {...}},
  "sheetOrder": [sheet_id, ...],
  "sheets": {sheet_id: {"id", "name", "rowCount", "columnCount",
                        "cellData": {row: {col: {"v": value, "f": "=FORMULA", "s": style_id}}},
                        "columnData": {col: {"w": px}}, "mergeData": [...]}}
}
Row/column keys are 0-based and may be ints or strings (after JSON round trips).
"""
import os
import re
import uuid
from decimal import ROUND_UP, Decimal

from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------- currencies

# IDR per 1 unit of currency, used when the vendor has no rate for the quote's currency.
# Values follow the company's Excel price templates. Override on the server with
# VQ_EXCHANGE_RATES="EUR:22000,USD:18500,INR:200".
DEFAULT_RATES = {"EUR": 22000.0, "USD": 18500.0, "INR": 200.0, "IDR": 1.0}


def default_rates():
    rates = dict(DEFAULT_RATES)
    for part in (os.getenv("VQ_EXCHANGE_RATES") or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                rates[k.strip().upper()] = float(v)
            except ValueError:
                pass
    return rates


_CUR_ALIASES = {
    "RS": "INR", "RS.": "INR", "RUPEE": "INR", "RUPEES": "INR", "\u20b9": "INR", "INR.": "INR",
    "$": "USD", "US$": "USD", "USD$": "USD", "US DOLLAR": "USD", "US DOLLARS": "USD", "DOLLAR": "USD",
    "\u20ac": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "RP": "IDR", "RP.": "IDR", "RUPIAH": "IDR",
}


def normalize_currency(value):
    """'Rs.', '\u20b9', 'inr' -> 'INR'; '$', 'US$' -> 'USD'; '\u20ac' -> 'EUR'. Unknown -> upper-cased text."""
    t = re.sub(r"\s+", " ", str(value or "")).strip().upper()
    if not t:
        return ""
    return _CUR_ALIASES.get(t, t)[:10]


def resolve_rate(currency, vendor=None, current=None, current_currency=None):
    """Exchange rate (IDR per 1 unit) for a quote currency.
    Order: rate already typed in the sheet for the same currency -> vendor default
    (only if the vendor's currency matches) -> company default for that currency."""
    cur = normalize_currency(currency)
    if current and float(current) > 0 and normalize_currency(current_currency) == cur:
        return float(current)
    if vendor is not None and normalize_currency(getattr(vendor, "currency", "")) == cur:
        v = float(getattr(vendor, "default_exchange_rate", 0) or 0)
        if v > 0:
            return v
    return float(default_rates().get(cur, 0.0))


# ---------------------------------------------------------------- basics

def col_letter(c):
    return get_column_letter(int(c) + 1)


def a1(r, c):
    return f"{col_letter(c)}{int(r) + 1}"


def new_id():
    return uuid.uuid4().hex[:12]


def iter_cells(sheet):
    for r, row in (sheet.get("cellData") or {}).items():
        if not isinstance(row, dict):
            continue
        for c, cell in row.items():
            if isinstance(cell, dict):
                yield int(r), int(c), cell


def get_cell(sheet, r, c):
    row = (sheet.get("cellData") or {}).get(str(r)) or (sheet.get("cellData") or {}).get(r) or {}
    return row.get(str(c)) or row.get(c) or {}


def cell_text(cell):
    v = cell.get("v")
    if v is None:
        p = cell.get("p")  # rich text
        if isinstance(p, dict):
            body = (p.get("body") or {}).get("dataStream", "")
            return body.replace("\r", "").replace("\n", " ").strip()
        return ""
    return str(v).strip()


def cell_number(cell):
    v = cell.get("v")
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def first_sheet(snapshot):
    order = snapshot.get("sheetOrder") or list((snapshot.get("sheets") or {}).keys())
    if not order:
        return None
    return snapshot["sheets"].get(order[0])


# ---------------------------------------------------------------- standard template
#
# Layout (Excel rows/columns, everything stays editable):
#   Row 1        title
#   Rows 2-6     B/C  Enquiry No., Customer, Vendor, Vendor Quote No., Currency
#   Rows 2-8     F:H label / I value: Exchange Rate, Margin 1, Margin 2, Margin 3,
#                Import Duty %, Import Duty (IDR), Vendor Quote Total
#   Row 10       item table headers, items from row 11
#   below items  TOTAL row, check row, PACKING block, CHARGES block (IDR)
#
# Import Duty (IDR) = Duty % x (total vendor invoice in IDR [Total Cost + Total Packing] + Total Charges)
#
# Per item row (x):
#   H Price after Discount     = F*(1-G)
#   I Cost per Unit (IDR)      = H*Rate
#   J Total Cost (IDR)         = I*Qty
#   K After Margin 1 (IDR)     = I/Margin1
#   L After Margin 2 (IDR)     = K/Margin2
#   M After Margin 3 (IDR)     = L/Margin3        (price after all margins)
#   N Line Value (IDR)         = M*Qty
#   O Freight Ratio            = N / total of N
#   P Total Freight (IDR)      = Ratio * Total Charges
#   Q Freight per Unit (IDR)   = P / Qty
#   R Total Duty (IDR)         = Ratio * Import Duty
#   S Duty per Unit (IDR)      = R / Qty
#   T Packing per Unit (IDR)   = packing of this item (+ share of a lot packing amount)
#   U Selling Price per Unit   = ROUNDUP(M+Q+S+T, -3)
#   V Total Selling Price      = U*Qty

STYLES = {
    "ttl": {"bl": 1, "fs": 13},
    "lbl": {"bl": 1, "cl": {"rgb": "#334155"}},
    "sec": {"bl": 1, "fs": 11, "cl": {"rgb": "#1E3A8A"}},
    "inp": {"bg": {"rgb": "#FEF9C3"}},
    "inpn": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "#,##0.00"}},
    "inpc": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "#,##0.00"}},       # input in quote currency
    "inpi": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "#,##0"}},
    "inpf": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "0.00"}},
    "pct": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "0%"}},
    "pctd": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "0.0%"}},
    "hdr": {"bl": 1, "bg": {"rgb": "#1E3A8A"}, "cl": {"rgb": "#FFFFFF"}, "tb": 3, "ht": 2, "vt": 2},
    "cur": {"n": {"pattern": "#,##0.00"}},
    "idr": {"n": {"pattern": "#,##0"}},
    "ratio": {"n": {"pattern": "0.0%"}},
    "sell": {"bl": 1, "bg": {"rgb": "#DCFCE7"}, "n": {"pattern": "#,##0"}},
    "tot": {"bl": 1, "bg": {"rgb": "#E2E8F0"}, "n": {"pattern": "#,##0"}},
    "totc": {"bl": 1, "bg": {"rgb": "#E2E8F0"}, "n": {"pattern": "#,##0.00"}},
    "totp": {"bl": 1, "bg": {"rgb": "#E2E8F0"}, "n": {"pattern": "0.0%"}},
    "totl": {"bl": 1, "bg": {"rgb": "#E2E8F0"}},
    "note": {"it": 1, "cl": {"rgb": "#64748B"}},
}

HEADERS = [
    ("No.", 45), ("Part No.", 125), ("Description", 260), ("Qty", 50), ("Unit", 55),
    ("Vendor Price per Unit ({cur})", 125), ("Discount %", 80), ("Price after Discount ({cur})", 125),
    ("Cost per Unit (IDR)", 125), ("Total Cost (IDR) (Cost per Unit × Qty)", 145),
    ("After Margin 1 (IDR)", 125), ("After Margin 2 (IDR)", 125), ("After Margin 3 (IDR) (Price after all Margins)", 150),
    ("Line Value (IDR) (After Margin 3 × Qty)", 150), ("Freight Ratio", 85),
    ("Total Freight (IDR) (Ratio × Total Charges)", 150), ("Freight per Unit (IDR)", 130),
    ("Total Duty (IDR) (Ratio × Import Duty)", 150), ("Duty per Unit (IDR)", 125),
    ("Packing per Unit (IDR)", 130), ("Selling Price per Unit (IDR)", 150), ("Total Selling Price (IDR)", 150),
]
HEADER_ROW = 9          # 0-based -> Excel row 10
FIRST_LINE_ROW = 10     # 0-based -> Excel row 11

# Settings cells (label merged F:H, value in I)
P_RATE, P_M1, P_M2, P_M3 = "$I$2", "$I$3", "$I$4", "$I$5"
P_DUTY_PCT, P_DUTY, P_VQ_TOTAL = "$I$6", "$I$7", "$I$8"

CHARGE_LABELS = [
    "Freight (Air / Sea)",
    "Custom Clearance Processing",
    "Port Demurrage Charges",
    "Port Warehouse Charges",
    "Local Transport",
    "Other Charges",
]
LOT_PACKING_LABEL = "Packing - one lot amount (if vendor gives only a total)"

# Amounts in the quote currency are shown the way the vendor writes them:
# INR in lakhs/crores (6,08,500.00), everything else 608,500.00.
INDIAN_NUMBER_FORMAT = "[>=10000000]##\\,##\\,##\\,##0.00;[>=100000]##\\,##\\,##0.00;##,##0.00"
STANDARD_NUMBER_FORMAT = "#,##0.00"


def currency_number_format(currency):
    return INDIAN_NUMBER_FORMAT if normalize_currency(currency) == "INR" else STANDARD_NUMBER_FORMAT


def _styles_for(currency):
    """Sheet styles with the quote-currency columns formatted for that currency."""
    import copy
    styles = copy.deepcopy(STYLES)
    fmt = currency_number_format(currency)
    for key in ("cur", "inpc", "totc"):
        styles[key]["n"] = {"pattern": fmt}
    return styles


def _num(x, default=0.0):
    if x is None or x == "":
        return default
    if isinstance(x, Decimal):
        return float(x)
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _roundup(x, digits):
    """Excel ROUNDUP (away from zero), e.g. digits=-3 -> up to the next thousand."""
    q = Decimal(1).scaleb(-digits)
    d = Decimal(str(round(x, 6)))
    return float(d.quantize(q, rounding=ROUND_UP)) if digits >= 0 else \
        float((d / q).to_integral_value(rounding=ROUND_UP) * q)


def build_standard_snapshot(*, enquiry_no, customer, vendor_name, vendor_quote_no, currency,
                            exchange_rate, margin1=1, margin2=1, margin3=1, discount=0,
                            lines=None, blank_lines=10, title=None, quote_total="",
                            lot_packing=0, charges=None, duty_pct=""):
    """Build the standard price calculation sheet.

    lines: [{part, description, qty, unit, unit_price, discount, packing_per_unit}]
           unit_price / packing_per_unit are in the quote currency.
    lot_packing: one packing amount for the whole quote (quote currency), shared by Freight Ratio.
    charges: {label: IDR amount} for the CHARGES block (all typed by the user in IDR).
    duty_pct: import duty as a fraction (0.05 = 5%); empty = no duty.
    """
    cur = normalize_currency(currency) or "EUR"
    lines = list(lines or [])
    charges = dict(charges or {})
    n = len(lines) if lines else blank_lines

    # ---- row positions (0-based) and Excel row numbers
    first = FIRST_LINE_ROW
    last = first + n - 1
    total_row = last + 1
    check_row = total_row + 1
    pk_title = check_row + 2
    pk_head = pk_title + 1
    pk_first = pk_head + 1                     # one packing row per item, same order
    pk_lot = pk_first + n
    pk_total = pk_lot + 1
    ch_title = pk_total + 2
    ch_head = ch_title + 1
    ch_first = ch_head + 1
    ch_total = ch_first + len(CHARGE_LABELS)
    X = lambda r: r + 1                        # 0-based row -> Excel row number
    F1, FL, T = X(first), X(last), X(total_row)
    PKL, PKT, CHT = X(pk_lot), X(pk_total), X(ch_total)
    R_CHARGES, R_LOT, R_PKTOT = f"$J${CHT}", f"$J${PKL}", f"$J${PKT}"

    cells, merges = {}, []

    def put(r, c, v=None, f=None, s=None):
        cell = {}
        if f is not None:
            cell["f"] = f
        if v is not None:
            cell["v"] = v
        if s:
            cell["s"] = s
        cells.setdefault(str(r), {})[str(c)] = cell

    # ---- title + quote details
    put(0, 0, f"Price Calculation - {vendor_name or ''} {vendor_quote_no or ''}".strip(" -"), s="ttl")
    for i, (label, value) in enumerate([
        ("Enquiry No.", enquiry_no or ""), ("Customer", customer or ""), ("Vendor", vendor_name or ""),
        ("Vendor Quote No.", vendor_quote_no or ""), ("Currency", cur),
    ]):
        put(1 + i, 1, label, s="lbl")
        put(1 + i, 2, value, s="inp")

    # ---- pre-compute every value (same maths as the formulas) so prices show at once
    rate_v = _num(exchange_rate)
    m1, m2, m3 = (_num(margin1, 1) or 1), (_num(margin2, 1) or 1), (_num(margin3, 1) or 1)
    duty_in = "" if duty_pct in (None, "") else _num(duty_pct)
    lot_v = _num(lot_packing)
    ch_vals = [_num(charges.get(lbl)) for lbl in CHARGE_LABELS]
    ch_sum = sum(ch_vals)
    rows = []
    for i in range(n):
        ln = lines[i] if i < len(lines) else {}
        qty = _num(ln.get("qty"))
        price = _num(ln.get("unit_price"))
        disc = _num(ln.get("discount"), _num(discount)) if ln else _num(discount)
        pk = _num(ln.get("packing_per_unit"))
        h = price * (1 - disc)
        ic = h * rate_v
        k1 = ic / m1
        k2 = k1 / m2
        k3 = k2 / m3
        rows.append({"qty": qty, "h": h, "i": ic, "j": ic * qty, "k1": k1, "k2": k2, "k3": k3,
                     "line": k3 * qty, "pk": pk, "pk_idr": pk * rate_v})
    l_tot = sum(r["line"] for r in rows)
    pk_tot_cur = sum(r["pk"] * r["qty"] for r in rows) + lot_v
    pk_tot_idr = pk_tot_cur * rate_v
    duty_v = _num(duty_in) * (sum(r["j"] for r in rows) + pk_tot_idr + ch_sum)
    for r in rows:
        q = r["qty"]
        r["ratio"] = r["line"] / l_tot if l_tot else 0
        r["frt"] = r["ratio"] * ch_sum
        r["frt_u"] = r["frt"] / q if q else 0
        r["duty"] = r["ratio"] * duty_v
        r["duty_u"] = r["duty"] / q if q else 0
        r["pk_u"] = r["pk_idr"] + (r["ratio"] * lot_v * rate_v / q if q else 0)
        r["sell"] = _roundup(r["k3"] + r["frt_u"] + r["duty_u"] + r["pk_u"], -3)
        r["sell_tot"] = r["sell"] * q

    # ---- settings
    for i, (label, value, style, note, formula) in enumerate([
        (f"Exchange Rate (IDR per 1 {cur})", rate_v, "inpn", "", None),
        ("Margin 1", m1, "inpf", "After Margin 1 = Cost / Margin 1, then / Margin 2, then / Margin 3", None),
        ("Margin 2 (1 = not used)", m2, "inpf", "", None),
        ("Margin 3 (1 = not used)", m3, "inpf", "", None),
        ("Import Duty % (empty = no duty)", duty_in, "pctd", "", None),
        ("Import Duty (IDR)", duty_v, "tot",
         "Import Duty = Duty % x (Total Cost + Total Packing + Total Charges)",
         f'=IF({P_DUTY_PCT}="",0,{P_DUTY_PCT}*(SUM(J{F1}:J{FL})+{R_PKTOT}+{R_CHARGES}))'),
        (f"Vendor Quote Total ({cur}, for the check)", "" if quote_total in (None, "") else _num(quote_total), "inpc", "", None),
    ]):
        r = 1 + i
        put(r, 5, label, s="lbl")
        put(r, 6, "", s="lbl")
        put(r, 7, "", s="lbl")
        merges.append({"startRow": r, "endRow": r, "startColumn": 5, "endColumn": 7})
        put(r, 8, value, f=formula, s=style)
        if note:
            put(r, 9, note, s="note")

    # ---- item table
    for c, (label, _w) in enumerate(HEADERS):
        put(HEADER_ROW, c, label.format(cur=cur), s="hdr")
    for i in range(n):
        r, x, v = first + i, X(first + i), rows[i]
        ln = lines[i] if i < len(lines) else {}
        pkx = X(pk_first + i)
        put(r, 0, i + 1)
        put(r, 1, ln.get("part", ""))
        put(r, 2, ln.get("description", ""))
        put(r, 3, ln.get("qty", "") if ln else "")
        put(r, 4, ln.get("unit", "pcs") if ln else "")
        put(r, 5, ln.get("unit_price", "") if ln else "", s="cur")
        put(r, 6, _num(ln.get("discount"), _num(discount)) if ln else _num(discount), s="pct")
        put(r, 7, v["h"], f=f"=F{x}*(1-G{x})", s="cur")
        put(r, 8, v["i"], f=f"=H{x}*{P_RATE}", s="idr")
        put(r, 9, v["j"], f=f"=I{x}*D{x}", s="idr")
        put(r, 10, v["k1"], f=f"=I{x}/{P_M1}", s="idr")
        put(r, 11, v["k2"], f=f"=K{x}/{P_M2}", s="idr")
        put(r, 12, v["k3"], f=f"=L{x}/{P_M3}", s="idr")
        put(r, 13, v["line"], f=f"=M{x}*D{x}", s="idr")
        put(r, 14, v["ratio"], f=f"=IFERROR(N{x}/$N${T},0)", s="ratio")
        put(r, 15, v["frt"], f=f"=O{x}*{R_CHARGES}", s="idr")
        put(r, 16, v["frt_u"], f=f"=IFERROR(P{x}/D{x},0)", s="idr")
        put(r, 17, v["duty"], f=f"=O{x}*{P_DUTY}", s="idr")
        put(r, 18, v["duty_u"], f=f"=IFERROR(R{x}/D{x},0)", s="idr")
        put(r, 19, v["pk_u"], f=f"=I{pkx}+IFERROR(O{x}*{R_LOT}/D{x},0)", s="idr")
        put(r, 20, v["sell"], f=f"=ROUNDUP(M{x}+Q{x}+S{x}+T{x},-3)", s="sell")
        put(r, 21, v["sell_tot"], f=f"=U{x}*D{x}", s="idr")

    # ---- TOTAL row
    put(total_row, 0, "TOTAL", s="totl")
    for c in range(1, len(HEADERS)):
        put(total_row, c, "", s="totl")
    put(total_row, 9, sum(r["j"] for r in rows), f=f"=SUM(J{F1}:J{FL})", s="tot")
    put(total_row, 13, l_tot, f=f"=SUM(N{F1}:N{FL})", s="tot")
    put(total_row, 14, sum(r["ratio"] for r in rows), f=f"=SUM(O{F1}:O{FL})", s="totp")
    put(total_row, 15, sum(r["frt"] for r in rows), f=f"=SUM(P{F1}:P{FL})", s="tot")
    put(total_row, 17, sum(r["duty"] for r in rows), f=f"=SUM(R{F1}:R{FL})", s="tot")
    put(total_row, 19, sum(r["pk_u"] * r["qty"] for r in rows),
        f=f"=SUMPRODUCT(T{F1}:T{FL},D{F1}:D{FL})", s="tot")
    put(total_row, 21, sum(r["sell_tot"] for r in rows), f=f"=SUM(V{F1}:V{FL})", s="tot")

    # ---- check row: goods after discount + packing (quote currency) vs vendor quote total
    vendor_sum = sum(r["h"] * r["qty"] for r in rows) + pk_tot_cur
    check = "" if quote_total in (None, "") else ("OK" if round(vendor_sum - _num(quote_total), 2) == 0 else "CHECK")
    put(check_row, 2, f"Check: items after discount + packing ({cur}) = vendor quote total?", s="note")
    put(check_row, 7, vendor_sum, f=f"=SUMPRODUCT(H{F1}:H{FL},D{F1}:D{FL})+$H${PKT}", s="cur")
    put(check_row, 8, check, f=f'=IF({P_VQ_TOTAL}="","",IF(ROUND(H{X(check_row)}-{P_VQ_TOTAL},2)=0,"OK","CHECK"))', s="lbl")

    # ---- PACKING block (quote currency -> IDR), added to the selling price, no margin
    put(pk_title, 0, "PACKING - added to the selling price per unit (no margin)", s="sec")
    for c, label in [(0, "No."), (2, "Packing for"), (3, "Qty"), (5, f"Packing per Unit ({cur})"),
                     (7, f"Packing Total ({cur})"), (8, "Packing per Unit (IDR)"), (9, "Packing Total (IDR)")]:
        put(pk_head, c, label, s="hdr")
    for c in (1, 4, 6):
        put(pk_head, c, "", s="hdr")
    for i in range(n):
        r, x, v = pk_first + i, X(pk_first + i), rows[i]
        lx = X(first + i)
        ln = lines[i] if i < len(lines) else {}
        put(r, 0, i + 1, f=f"=A{lx}")
        put(r, 2, ln.get("description", "") or ln.get("part", ""), f=f'=IF(C{lx}="",B{lx},C{lx})')
        put(r, 3, v["qty"], f=f"=D{lx}")
        put(r, 5, ln.get("packing_per_unit", "") if ln and ln.get("packing_per_unit") not in (None, "") else 0, s="inpc")
        put(r, 7, v["pk"] * v["qty"], f=f"=F{x}*D{x}", s="cur")
        put(r, 8, v["pk_idr"], f=f"=F{x}*{P_RATE}", s="idr")
        put(r, 9, v["pk_idr"] * v["qty"], f=f"=I{x}*D{x}", s="idr")
    put(pk_lot, 2, LOT_PACKING_LABEL)
    put(pk_lot, 7, lot_v, s="inpc")
    put(pk_lot, 9, lot_v * rate_v, f=f"=H{PKL}*{P_RATE}", s="idr")
    put(pk_total, 2, "Total Packing", s="totl")
    for c in (0, 1, 3, 4, 5, 6, 8):
        put(pk_total, c, "", s="totl")
    put(pk_total, 7, pk_tot_cur, f=f"=SUM(H{X(pk_first)}:H{PKL})", s="totc")
    put(pk_total, 9, pk_tot_idr, f=f"=SUM(J{X(pk_first)}:J{PKL})", s="tot")

    # ---- CHARGES block (IDR, typed by the user), shared over the items by Freight Ratio
    put(ch_title, 0, "CHARGES (IDR) - shared over the items by Freight Ratio (no margin)", s="sec")
    put(ch_head, 2, "Charge", s="hdr")
    put(ch_head, 9, "Amount (IDR)", s="hdr")
    for c in (0, 1, 3, 4, 5, 6, 7, 8):
        put(ch_head, c, "", s="hdr")
    for i, label in enumerate(CHARGE_LABELS):
        put(ch_first + i, 2, label)
        put(ch_first + i, 9, ch_vals[i], s="inpi")
    put(ch_total, 2, "Total Charges", s="totl")
    for c in (0, 1, 3, 4, 5, 6, 7, 8):
        put(ch_total, c, "", s="totl")
    put(ch_total, 9, ch_sum, f=f"=SUM(J{X(ch_first)}:J{X(ch_total - 1)})", s="tot")

    if rate_v <= 0:
        put(ch_total + 2, 2, f"Exchange rate for {cur} is 0 - type the IDR rate in I2.", s="note")

    sid = new_id()
    return {
        "id": new_id(),
        "name": title or f"{enquiry_no or 'Quote'}",
        "appVersion": "1.4.0",
        "locale": "enUS",
        "styles": _styles_for(cur),
        "sheetOrder": [sid],
        "sheets": {
            sid: {
                "id": sid,
                "name": "Price Table",
                "rowCount": max(60, ch_total + 20),
                "columnCount": 26,
                "defaultColumnWidth": 90,
                "defaultRowHeight": 24,
                "freeze": {"xSplit": 0, "ySplit": HEADER_ROW + 1, "startRow": HEADER_ROW + 1, "startColumn": 0},
                "rowData": {str(HEADER_ROW): {"h": 60}, str(pk_head): {"h": 40}},
                "columnData": {str(c): {"w": w} for c, (_l, w) in enumerate(HEADERS)},
                "mergeData": merges,
                "cellData": cells,
            }
        },
    }


# ---------------------------------------------------------------- reading settings back

_PARAM_KEYS = [
    ("exchange rate", "rate"),
    ("margin 1", "m1"), ("margin factor", "m1"),
    ("margin 2", "m2"), ("duty factor", "m2"),          # older sheets: duty factor was a 2nd divisor
    ("margin 3", "m3"), ("freight factor", "m3"),       # older sheets: freight factor was a 3rd divisor
    ("vendor quote total", "vq_total"),
    ("import duty %", "duty_pct"),
    ("freight / courier", "legacy_freight"),            # older sheets: freight in quote currency
]


def read_settings(sheet):
    """Read the settings, currency and IDR charges of a standard sheet (new or older layout).
    Returns {} when the sheet isn't a standard sheet (e.g. a copied vendor master sheet)."""
    if not sheet:
        return {}
    out = {}
    for r in range(0, HEADER_ROW):
        for label_col, value_col in ((5, 8), (7, 9)):           # new layout F->I, older H->J
            label = _norm(cell_text(get_cell(sheet, r, label_col)))
            for prefix, key in _PARAM_KEYS:
                if label.startswith(prefix) and key not in out:
                    num = cell_number(get_cell(sheet, r, value_col))
                    if num is not None:
                        out[key] = num
        if _norm(cell_text(get_cell(sheet, r, 1))) == "currency":
            out["currency"] = normalize_currency(cell_text(get_cell(sheet, r, 2)))
    if "rate" not in out and "m1" not in out:
        return {}
    wanted = {_norm(lbl): lbl for lbl in CHARGE_LABELS}
    charges = {}
    for r, c, cell in iter_cells(sheet):
        if c == 2 and _norm(cell_text(cell)) in wanted:
            num = cell_number(get_cell(sheet, r, 9))
            if num:
                charges[wanted[_norm(cell_text(cell))]] = num
    out["charges"] = charges
    return out


def is_standard_layout(sheet):
    """True when the sheet is one of our standard sheets (new or older layout)."""
    return bool(read_settings(sheet))


def fresh_copy(snapshot, *, title=None):
    """Deep copy a snapshot with new ids so Univer treats it as a new workbook."""
    import copy
    snap = copy.deepcopy(snapshot or {})
    old_sheets = snap.get("sheets") or {}
    order = snap.get("sheetOrder") or list(old_sheets.keys())
    new_sheets, new_order = {}, []
    for sid in order:
        sh = old_sheets.get(sid)
        if not sh:
            continue
        nid = new_id()
        sh["id"] = nid
        new_sheets[nid] = sh
        new_order.append(nid)
    snap["sheets"], snap["sheetOrder"] = new_sheets, new_order
    snap["id"] = new_id()
    if title:
        snap["name"] = title
    return snap


# ---------------------------------------------------------------- reading lines for Zoho

def _norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def find_price_table(sheet):
    """Locate header row + columns of a price table by header text. Works for the
    standard template and most hand-made BOS-style sheets."""
    rows = {}
    for r, c, cell in iter_cells(sheet):
        t = _norm(cell_text(cell))
        if t:
            rows.setdefault(r, {})[c] = t
    for r in sorted(rows):
        heads = rows[r]
        price_col = next((c for c, t in heads.items() if "selling" in t and "total" not in t), None)
        if price_col is None:
            continue
        qty_col = next((c for c, t in heads.items() if t in ("qty", "qty.", "quantity") or t.startswith("qty")), None)
        desc_col = next((c for c, t in heads.items() if "description" in t), None)
        part_col = next((c for c, t in heads.items() if t.startswith("part") or "part no" in t or t == "item"), None)
        unit_col = next((c for c, t in heads.items() if t in ("unit", "uom")), None)
        if qty_col is None or (desc_col is None and part_col is None):
            continue
        return {"header_row": r, "price": price_col, "qty": qty_col, "desc": desc_col,
                "part": part_col, "unit": unit_col}
    return None


def extract_lines(snapshot):
    """Return (lines, problems). Each line: part, description, qty, unit, rate (IDR)."""
    sheet = first_sheet(snapshot)
    if not sheet:
        return [], ["The workbook has no sheets."]
    layout = find_price_table(sheet)
    if not layout:
        return [], ["Couldn't find the price table. Keep header cells named 'Qty', 'Description' and 'Unit Selling Price'."]
    lines, problems = [], []
    max_row = max([r for r, _c, _x in iter_cells(sheet)] + [layout["header_row"]])
    for r in range(layout["header_row"] + 1, max_row + 1):
        first = _norm(cell_text(get_cell(sheet, r, 0)))
        if first.startswith("total"):
            break
        part = cell_text(get_cell(sheet, r, layout["part"])) if layout["part"] is not None else ""
        desc = cell_text(get_cell(sheet, r, layout["desc"])) if layout["desc"] is not None else ""
        if not part and not desc:
            continue
        price_cell = get_cell(sheet, r, layout["price"])
        qty = cell_number(get_cell(sheet, r, layout["qty"]))
        rate = cell_number(price_cell)
        if price_cell.get("f") and price_cell.get("v") in (None, ""):
            if qty:
                problems.append(f"Row {r + 1}: selling price not calculated - open the sheet and save it once.")
            continue
        if not qty or qty <= 0 or rate is None:
            continue
        if rate <= 0:
            problems.append(f"Row {r + 1}: selling price is 0 - check the exchange rate and the unit price.")
            continue
        unit = cell_text(get_cell(sheet, r, layout["unit"])) if layout["unit"] is not None else ""
        lines.append({"part": part, "description": desc, "qty": qty, "unit": unit, "rate": round(rate, 2)})
    if not lines and not problems:
        problems.append("No priced lines found (need a quantity and a unit selling price).")
    return lines, problems


# ---------------------------------------------------------------- change log

def _cell_repr(cell):
    if not cell:
        return ""
    if cell.get("f"):
        return str(cell["f"])
    v = cell.get("v")
    return "" if v is None else str(v)


def diff_snapshots(old, new, limit=500):
    """List of (sheet_name, A1, old, new) for cells whose value or formula changed."""
    def by_name(snap):
        out = {}
        for sid, sh in (snap.get("sheets") or {}).items():
            cells = {}
            for r, c, cell in iter_cells(sh):
                rep = _cell_repr(cell)
                if rep != "":
                    cells[(r, c)] = rep
            out[sh.get("name") or sid] = cells
        return out
    a, b = by_name(old or {}), by_name(new or {})
    changes = []
    for name in sorted(set(a) | set(b)):
        ca, cb = a.get(name, {}), b.get(name, {})
        for key in sorted(set(ca) | set(cb)):
            if ca.get(key, "") != cb.get(key, ""):
                changes.append((name, a1(*key), ca.get(key, ""), cb.get(key, "")))
                if len(changes) >= limit:
                    return changes
    return changes
