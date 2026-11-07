"""
Excel <-> sheet snapshot conversion.

- import_master(): split a vendor master workbook into one snapshot per sheet and
  read ENQ no. / customer / quote numbers from the top of each sheet.
- snapshot_to_worksheet(): write a snapshot back as a worksheet with live formulas.
- append_to_master(): add an approved price table as a new sheet in a copy of the
  vendor's current master workbook (the previous version is kept unchanged).
"""
import datetime
import io
import re

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from .sheet import iter_cells, new_id, cell_text

ENQ_RE = re.compile(r"ENQ\s*/\s*\d{4}\s*/\s*\d{1,2}\s*/\s*\d+", re.I)
QTN_RE = re.compile(r"quotation\s*no\.?\s*[:#]?\s*([A-Z0-9_\-/]+R?\d*)", re.I)
VQ_HEAD_RE = re.compile(r"(quote|quotation)\s*no", re.I)
INVALID_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")


# ---------------------------------------------------------------- import

def _value_for_json(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()[:10] if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime) else v.isoformat()
    if isinstance(v, datetime.time):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _clean(s):
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def worksheet_to_snapshot_sheet(ws_f, ws_v, styles):
    """Convert one worksheet (formula view + cached-value view) to a Univer sheet dict."""
    cells = {}
    max_r, max_c = 0, 0
    style_ids = {}
    for row in ws_f.iter_rows():
        for cell in row:
            raw = cell.value
            if raw is None:
                continue
            r, c = cell.row - 1, cell.column - 1
            entry = {}
            if isinstance(raw, str) and raw.startswith("="):
                entry["f"] = raw
                cached = ws_v.cell(row=cell.row, column=cell.column).value if ws_v is not None else None
                if cached is not None:
                    entry["v"] = _value_for_json(cached)
            elif hasattr(raw, "text") and not isinstance(raw, str):   # ArrayFormula
                entry["f"] = "=" + str(raw.text).lstrip("=")
            else:
                entry["v"] = _value_for_json(raw)
            # minimal styling: bold + number format
            key = (bool(cell.font and cell.font.b), cell.number_format if cell.number_format != "General" else "")
            if key != (False, ""):
                if key not in style_ids:
                    sid = f"x{len(styles) + 1}"
                    st = {}
                    if key[0]:
                        st["bl"] = 1
                    if key[1]:
                        st["n"] = {"pattern": key[1]}
                    styles[sid] = st
                    style_ids[key] = sid
                entry["s"] = style_ids[key]
            cells.setdefault(str(r), {})[str(c)] = entry
            max_r, max_c = max(max_r, r), max(max_c, c)
    col_data = {}
    for letter, dim in ws_f.column_dimensions.items():
        if dim.width:
            try:
                idx = openpyxl.utils.column_index_from_string(letter) - 1
            except ValueError:
                continue
            col_data[str(idx)] = {"w": int(dim.width * 7 + 5)}
    merges = []
    for rng in ws_f.merged_cells.ranges:
        merges.append({"startRow": rng.min_row - 1, "endRow": rng.max_row - 1,
                       "startColumn": rng.min_col - 1, "endColumn": rng.max_col - 1})
    sid = new_id()
    return {
        "id": sid,
        "name": ws_f.title[:31],
        "rowCount": max(60, max_r + 30),
        "columnCount": max(20, max_c + 5),
        "defaultColumnWidth": 90,
        "defaultRowHeight": 22,
        "cellData": cells,
        "columnData": col_data,
        "mergeData": merges,
    }


def read_sheet_details(sheet):
    """Pull enquiry no., customer, vendor quote no. and our quotation no. from a sheet."""
    texts = []
    for r, c, cell in iter_cells(sheet):
        t = _clean(cell_text(cell))
        if t:
            texts.append((r, c, t))
    texts.sort()
    top = [t for t in texts if t[0] <= 6]
    enquiry = ""
    for _r, _c, t in top:
        m = ENQ_RE.search(t)
        if m:
            enquiry = re.sub(r"\s+", "", m.group(0)).upper()
            break
    quotation = ""
    for _r, _c, t in top:
        m = QTN_RE.search(t)
        if m and not t.lower().startswith(("bos", "jenco", "vendor")):
            quotation = m.group(1)
            break
    customer = ""
    for _r, _c, t in top:
        if re.match(r"^(PT\.?|CV\.?)\s", t, re.I) or " Tbk" in t:
            customer = t
            break
    vendor_quote = ""
    for r, c, t in texts:
        if VQ_HEAD_RE.search(t) and not t.lower().startswith("quotation no"):
            # value either after the label in the same cell or in the first cell below
            tail = VQ_HEAD_RE.split(t)[-1].strip(" .:#")
            if re.search(r"\d{4,}", tail) and not ENQ_RE.search(tail):
                vendor_quote = re.search(r"[A-Z0-9\-/]*\d{4,}[A-Z0-9\-/]*", tail).group(0)
                break
            below = [x for x in texts if x[1] == c and x[0] > r]
            if below and re.search(r"\d{4,}", below[0][2]) and not ENQ_RE.search(below[0][2]):
                vendor_quote = below[0][2]
                break
    return {"enquiry_no": enquiry, "customer_name": customer[:255],
            "vendor_quote_no": vendor_quote[:100], "quotation_no": quotation[:100]}


def import_master(fileobj):
    """Return list of dicts (one per sheet): name, position, snapshot, details."""
    data = fileobj.read()
    wb_f = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    try:
        wb_v = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception:
        wb_v = None
    out = []
    for pos, ws in enumerate(wb_f.worksheets):
        if ws.max_row <= 1 and ws.max_column <= 1 and ws.cell(1, 1).value is None:
            continue
        styles = {}
        sheet = worksheet_to_snapshot_sheet(ws, wb_v[ws.title] if wb_v else None, styles)
        if not sheet["cellData"]:
            continue
        snapshot = {"id": new_id(), "name": ws.title, "appVersion": "1.4.0", "locale": "enUS",
                    "styles": styles, "sheetOrder": [sheet["id"]], "sheets": {sheet["id"]: sheet}}
        details = read_sheet_details(sheet)
        out.append({"name": ws.title, "position": pos, "snapshot": snapshot, "details": details})
    return out


# ---------------------------------------------------------------- export

def _hex(rgb):
    if not rgb:
        return None
    rgb = rgb.lstrip("#")
    return rgb.upper() if len(rgb) == 6 else None


def snapshot_to_worksheet(snapshot, ws, sheet_id=None):
    order = snapshot.get("sheetOrder") or list((snapshot.get("sheets") or {}).keys())
    sheet = snapshot["sheets"][sheet_id or order[0]]
    styles = snapshot.get("styles") or {}
    for r, c, cell in iter_cells(sheet):
        target = ws.cell(row=r + 1, column=c + 1)
        if cell.get("f"):
            f = str(cell["f"])
            target.value = f if f.startswith("=") else "=" + f
        else:
            v = cell.get("v")
            if v is None:
                t = cell_text(cell)
                v = t or None
            target.value = v
        st = cell.get("s")
        st = styles.get(st) if isinstance(st, str) else (st if isinstance(st, dict) else None)
        if st:
            color = _hex((st.get("cl") or {}).get("rgb"))
            if st.get("bl") or st.get("it") or color:
                target.font = Font(bold=bool(st.get("bl")), italic=bool(st.get("it")), color=color)
            bg = _hex((st.get("bg") or {}).get("rgb"))
            if bg:
                target.fill = PatternFill("solid", fgColor=bg)
            pattern = (st.get("n") or {}).get("pattern")
            if pattern:
                target.number_format = pattern
            if st.get("tb") == 3 or st.get("ht") == 2:
                target.alignment = Alignment(wrap_text=st.get("tb") == 3,
                                             horizontal="center" if st.get("ht") == 2 else None,
                                             vertical="center")
    for c, info in (sheet.get("columnData") or {}).items():
        w = (info or {}).get("w")
        if w:
            ws.column_dimensions[get_column_letter(int(c) + 1)].width = max(4, round(w / 7, 1))
    for m in sheet.get("mergeData") or []:
        try:
            ws.merge_cells(start_row=m["startRow"] + 1, end_row=m["endRow"] + 1,
                           start_column=m["startColumn"] + 1, end_column=m["endColumn"] + 1)
        except Exception:
            pass
    fr = sheet.get("freeze") or {}
    if fr.get("ySplit"):
        ws.freeze_panes = ws.cell(row=fr["ySplit"] + 1, column=1)
    return ws


def safe_sheet_title(base, existing):
    base = INVALID_SHEET_CHARS.sub(" ", base).strip()[:31] or "Quote"
    title, i = base, 2
    lower = {t.lower() for t in existing}
    while title.lower() in lower:
        suffix = f" ({i})"
        title = base[:31 - len(suffix)] + suffix
        i += 1
    return title


def build_single_workbook(snapshot, title):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = safe_sheet_title(title, [])
    snapshot_to_worksheet(snapshot, ws)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def append_to_master(master_bytes, snapshot, title):
    """Return (new_workbook_bytes, sheet_title, sheet_count)."""
    if master_bytes:
        wb = openpyxl.load_workbook(io.BytesIO(master_bytes))
    else:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
    sheet_title = safe_sheet_title(title, wb.sheetnames)
    ws = wb.create_sheet(sheet_title)
    snapshot_to_worksheet(snapshot, ws)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), sheet_title, len(wb.sheetnames)
