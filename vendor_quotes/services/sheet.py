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
import re
import uuid
from decimal import Decimal

from openpyxl.utils import get_column_letter

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

STYLES = {
    "ttl": {"bl": 1, "fs": 13},
    "lbl": {"bl": 1, "cl": {"rgb": "#334155"}},
    "inp": {"bg": {"rgb": "#FEF9C3"}},
    "inpn": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "#,##0.00"}},
    "inpf": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "0.00"}},
    "hdr": {"bl": 1, "bg": {"rgb": "#1E3A8A"}, "cl": {"rgb": "#FFFFFF"}, "tb": 3, "ht": 2, "vt": 2},
    "cur": {"n": {"pattern": "#,##0.00"}},
    "pct": {"bg": {"rgb": "#FEF9C3"}, "n": {"pattern": "0%"}},
    "idr": {"n": {"pattern": "#,##0"}},
    "sell": {"bl": 1, "bg": {"rgb": "#DCFCE7"}, "n": {"pattern": "#,##0"}},
    "tot": {"bl": 1, "bg": {"rgb": "#E2E8F0"}, "n": {"pattern": "#,##0"}},
    "totl": {"bl": 1, "bg": {"rgb": "#E2E8F0"}},
    "note": {"it": 1, "cl": {"rgb": "#64748B"}},
}

HEADERS = [
    ("Line", 50), ("Part No.", 110), ("Description", 260), ("Qty", 55), ("Unit", 55),
    ("Unit Price ({cur})", 110), ("Discount", 75), ("Net Price ({cur})", 110),
    ("Net Price (IDR)", 120), ("Cost after factors (IDR)", 150), ("Line Cost (IDR)", 130),
    ("Freight Share (IDR)", 130), ("Unit Selling Price (IDR)", 160), ("Total Selling (IDR)", 150),
]
HEADER_ROW = 7          # 0-based -> Excel row 8
FIRST_LINE_ROW = 8      # 0-based -> Excel row 9

# Parameter cells (Excel refs) used by the formulas
P_RATE, P_MARGIN, P_DUTY, P_FREIGHT_F, P_FREIGHT, P_VQ_TOTAL = "$J$1", "$J$2", "$J$3", "$J$4", "$J$5", "$J$6"


def _num(x, default=0.0):
    if x is None or x == "":
        return default
    if isinstance(x, Decimal):
        return float(x)
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_standard_snapshot(*, enquiry_no, customer, vendor_name, vendor_quote_no, currency,
                            exchange_rate, margin_factor, duty_factor, freight_factor,
                            discount, lines=None, blank_lines=10, title=None):
    """Build the standard (BOS spare-parts style) price table. Everything stays editable.

    Rows 1-6: quote details (left, B/C) and pricing factors (right, H:I label, J value).
    Row 8: column headers. Row 9+: lines. Then TOTAL and a check row.
    """
    cur = currency or "EUR"
    lines = list(lines or [])
    n = len(lines) if lines else blank_lines
    last = FIRST_LINE_ROW + n - 1               # 0-based last line row
    total_row = last + 1
    check_row = total_row + 1
    L, T = last + 1, total_row + 1              # Excel row numbers
    F1 = FIRST_LINE_ROW + 1

    cells = {}
    merges = []

    def put(r, c, v=None, f=None, s=None):
        cell = {}
        if f is not None:
            cell["f"] = f
        if v is not None:
            cell["v"] = v
        if s:
            cell["s"] = s
        cells.setdefault(str(r), {})[str(c)] = cell

    # Quote details (left block)
    for r, (label, value) in enumerate([
        ("Enquiry No.", enquiry_no or ""), ("Customer", customer or ""), ("Vendor", vendor_name or ""),
        ("Vendor Quote No.", vendor_quote_no or ""), ("Currency", cur),
    ]):
        put(r, 1, label, s="lbl")
        put(r, 2, value, s="inp")
    # Pricing factors (right block): label in H:I (merged), value in J
    for r, (label, value, style) in enumerate([
        (f"Exchange rate (IDR per 1 {cur})", _num(exchange_rate), "inpn"),
        ("Margin factor (cost ÷ this)", _num(margin_factor, 1), "inpf"),
        ("Duty factor (0.9 = 10% duty)", _num(duty_factor, 1), "inpf"),
        ("Freight factor", _num(freight_factor, 1), "inpf"),
        (f"Freight / courier ({cur}, spread on lines)", 0, "inpn"),
        (f"Vendor quote total ({cur}, for the check)", "", "inpn"),
    ]):
        put(r, 7, label, s="lbl")
        put(r, 8, "", s="lbl")
        merges.append({"startRow": r, "endRow": r, "startColumn": 7, "endColumn": 8})
        put(r, 9, value, s=style)

    for c, (label, _w) in enumerate(HEADERS):
        put(HEADER_ROW, c, label.format(cur=cur), s="hdr")

    for i in range(n):
        r = FIRST_LINE_ROW + i
        x = r + 1  # Excel row
        line = lines[i] if i < len(lines) else {}
        put(r, 0, i + 1)
        put(r, 1, line.get("part", ""))
        put(r, 2, line.get("description", ""))
        put(r, 3, line.get("qty", "") if line else "")
        put(r, 4, line.get("unit", "pcs") if line else "")
        put(r, 5, line.get("unit_price", "") if line else "", s="cur")
        put(r, 6, _num(line.get("discount"), _num(discount)) if line else _num(discount), s="pct")
        put(r, 7, f=f'=IF(F{x}="","",F{x}*(1-G{x}))', s="cur")
        put(r, 8, f=f'=IF(H{x}="","",H{x}*{P_RATE})', s="idr")
        put(r, 9, f=f'=IF(I{x}="","",I{x}/{P_MARGIN}/{P_DUTY}/{P_FREIGHT_F})', s="idr")
        put(r, 10, f=f'=IF(OR(J{x}="",D{x}=""),"",J{x}*D{x})', s="idr")
        put(r, 11, f=f'=IF(OR(K{x}="",$K${T}=0),0,K{x}/$K${T}*{P_FREIGHT}*{P_RATE})', s="idr")
        put(r, 12, f=f'=IF(OR(K{x}="",D{x}=0),"",ROUND((K{x}+L{x})/D{x},-3))', s="sell")
        put(r, 13, f=f'=IF(M{x}="","",M{x}*D{x})', s="idr")

    put(total_row, 0, "TOTAL", s="totl")
    for c in range(1, 14):
        put(total_row, c, "", s="totl")
    put(total_row, 10, f=f"=SUM(K{F1}:K{L})", s="tot")
    put(total_row, 11, f=f"=SUM(L{F1}:L{L})", s="tot")
    put(total_row, 13, f=f"=SUM(N{F1}:N{L})", s="tot")

    put(check_row, 2, f"Check: net {cur} total vs vendor quote total", s="note")
    put(check_row, 7, f=f'=SUMPRODUCT(H{F1}:H{L},D{F1}:D{L})', s="cur")
    put(check_row, 8, f=f'=IF({P_VQ_TOTAL}="","",IF(ROUND(H{check_row + 1}-{P_VQ_TOTAL},2)=0,"OK","CHECK"))', s="lbl")

    sid = new_id()
    return {
        "id": new_id(),
        "name": title or f"{enquiry_no or 'Quote'}",
        "appVersion": "1.4.0",
        "locale": "enUS",
        "styles": STYLES,
        "sheetOrder": [sid],
        "sheets": {
            sid: {
                "id": sid,
                "name": "Price Table",
                "rowCount": max(60, check_row + 20),
                "columnCount": 20,
                "defaultColumnWidth": 90,
                "defaultRowHeight": 24,
                "freeze": {"xSplit": 0, "ySplit": HEADER_ROW + 1, "startRow": HEADER_ROW + 1, "startColumn": 0},
                "rowData": {str(HEADER_ROW): {"h": 40}},
                "columnData": {str(c): {"w": w} for c, (_l, w) in enumerate(HEADERS)},
                "mergeData": merges,
                "cellData": cells,
            }
        },
    }


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
