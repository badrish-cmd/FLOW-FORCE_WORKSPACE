"""Read PIDs from the LIST PID - PT FLOW FORCE INDONESIA table (read only)."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings

COLS = {
    "sno": "S_NO", "pid": "PID", "enquiry": "ENQUIRY_NO/QUOTATION_NO", "so": "SALES_ORDER", "po": "CUSTOMER PO",
    "date": "DATE", "company": "COMPANY_NAME", "desc_q": "DESCRIPTION_AS_QUOTATION", "desc_po": "DESCRIPTION_AS_PO",
    "qty": "QTY", "unit": "UNIT_MEASUREMENT", "due_customer": "DUE_DATE_CUSTOMER", "due_ff": "DUE_DATE_FLOW_FORCE",
    "kind": "SUPPLY/PROJECT", "invoice_address": "INVOICE_ADDRESS", "delivery_address": "DELIVERY_ADDRESS",
}


def pid_table():
    from tables.models import Table
    table_id = getattr(settings, "WORK_ORDER_PID_TABLE_ID", None)
    qs = Table.objects.filter(is_active=True, job_type="LIST_PID")
    if table_id:
        return qs.filter(pk=table_id).first()
    for t in qs.order_by("id"):
        name = t.name.upper()
        if "INDONESIA" in name and "ENGINEERING" not in name:
            return t
    return None


def _columns(table):
    by_name = {c.name.strip().upper(): c for c in table.columns.all()}
    return {k: by_name.get(v.upper()) for k, v in COLS.items()}


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _date(v):
    s = _s(v)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _num(v):
    s = _s(v).replace(",", ".")
    try:
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


def _values(table, cols, row_ids, keys):
    from tables.models import CellValue
    col_ids = {cols[k].id: k for k in keys if cols.get(k)}
    out = {}
    for cv in CellValue.objects.filter(row_id__in=row_ids, column_id__in=col_ids).values("row_id", "column_id", "value"):
        out.setdefault(cv["row_id"], {})[col_ids[cv["column_id"]]] = cv["value"]
    return out


def recent_pids(limit=300):
    """[(pid, company)] newest first, for the PID picker."""
    from tables.models import CellValue
    table = pid_table()
    if not table:
        return []
    cols = _columns(table)
    if not cols.get("pid"):
        return []
    rows = (CellValue.objects.filter(column=cols["pid"], row__is_archived=False)
            .exclude(value__isnull=True).exclude(value="").order_by("-row_id").values("row_id", "value")[:limit * 4])
    seen, out = set(), []
    row_ids = []
    for r in rows:
        p = _s(r["value"])
        if p and p not in seen:
            seen.add(p)
            out.append((p, r["row_id"]))
            row_ids.append(r["row_id"])
        if len(out) >= limit:
            break
    comp = _values(table, cols, row_ids, ["company"]) if cols.get("company") else {}
    return [(p, _s(comp.get(rid, {}).get("company"))) for p, rid in out]


def load_pid(pid):
    """Header + item lines of a PID. Item rows right after it with an empty PID and the same customer PO
    belong to it too (that's how LIST PID is typed). Returns None if the PID isn't found."""
    from tables.models import CellValue, Row
    pid = _s(pid)
    table = pid_table()
    if not pid or not table:
        return None
    cols = _columns(table)
    if not cols.get("pid"):
        return None
    hits = list(CellValue.objects.filter(column=cols["pid"], row__table=table, row__is_archived=False,
                                         value__in=[pid, int(pid) if pid.isdigit() else pid])
                .values_list("row_id", flat=True))
    if not hits:
        return None
    # neighbours: rows that follow (by S_NO) with no PID but the same customer PO
    all_rows = list(Row.objects.filter(table=table, is_archived=False).values_list("id", flat=True))
    base = _values(table, cols, all_rows, ["sno", "pid", "po"])
    order = sorted(all_rows, key=lambda r: (float(_num(base.get(r, {}).get("sno")) or 0), r))
    picked = []
    hit_set = set(hits)
    pos = {rid: i for i, rid in enumerate(order)}
    for rid in sorted(hits, key=lambda r: pos.get(r, 0)):
        if rid not in picked:
            picked.append(rid)
        po = _s(base.get(rid, {}).get("po"))
        i = pos.get(rid, -1) + 1
        while 0 < i < len(order):
            nxt = order[i]
            b = base.get(nxt, {})
            if nxt in hit_set or _s(b.get("pid")) or _s(b.get("po")) != po:
                break
            if nxt not in picked:
                picked.append(nxt)
            i += 1
    data = _values(table, cols, picked, list(COLS))
    first = data.get(picked[0], {})
    head = {
        "pid": pid, "table_id": table.id, "row_ids": picked,
        "customer_name": _s(first.get("company")), "invoice_address": _s(first.get("invoice_address")),
        "delivery_address": _s(first.get("delivery_address")), "order_date": _date(first.get("date")),
        "required_by": _date(first.get("due_ff")) or _date(first.get("due_customer")),
        "customer_po": _s(first.get("po")), "quotation_no": "" if _s(first.get("enquiry")) in ("0", "-") else _s(first.get("enquiry")),
        "sales_order_no": _s(first.get("so")), "kind": _s(first.get("kind")),
    }
    lines = []
    for rid in picked:
        d = data.get(rid, {})
        desc = _s(d.get("desc_po")) or _s(d.get("desc_q"))
        if not desc:
            continue
        lines.append({"row_id": rid, "description": desc, "qty": _num(d.get("qty")), "unit": _s(d.get("unit"))})
    head["lines"] = lines
    return head
