"""Work Order PDF in the same layout as the Flow Force Sales Order (logo left, title right, item table)."""
import io
import os
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Flowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from xml.sax.saxutils import escape

LOGO = os.path.join(os.path.dirname(__file__), "assets", "flowforce_logo.png")
COMPANY = ["PT. Flow Force Indonesia", "Kawasan Industri Delta Silicon 5", "Jl. Kenari 1 Blok G1 No.23D",
           "Cikarang 17550", "Indonesia"]
DARK = colors.HexColor("#3c3c3c")
GREY = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d1d5db")
RED = colors.HexColor("#ec1c24")

S = {
    "n": ParagraphStyle("n", fontName="Helvetica", fontSize=8.5, leading=11),
    "b": ParagraphStyle("b", fontName="Helvetica-Bold", fontSize=8.5, leading=11),
    "small": ParagraphStyle("s", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=GREY),
    "title": ParagraphStyle("t", fontName="Helvetica", fontSize=24, leading=28, alignment=TA_RIGHT),
    "num": ParagraphStyle("num", fontName="Helvetica-Bold", fontSize=9.5, leading=12, alignment=TA_RIGHT),
    "r": ParagraphStyle("r", fontName="Helvetica", fontSize=8.5, leading=11, alignment=TA_RIGHT),
    "rb": ParagraphStyle("rb", fontName="Helvetica-Bold", fontSize=8.5, leading=11, alignment=TA_RIGHT),
    "h": ParagraphStyle("h", fontName="Helvetica", fontSize=10, leading=13, spaceBefore=8, spaceAfter=3, keepWithNext=1),
    "th": ParagraphStyle("th", fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.white),
    "thr": ParagraphStyle("thr", fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.white, alignment=TA_RIGHT),
    "label": ParagraphStyle("l", fontName="Helvetica", fontSize=8.5, leading=11, textColor=GREY, alignment=TA_RIGHT),
}


def P(text, style="n"):
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), S[style])


def _money(v):
    if v is None:
        return ""
    v = Decimal(v).quantize(Decimal("0.01"))
    return f"{v:,.2f}"


def _qty(v):
    if v is None:
        return ""
    v = Decimal(v).normalize()
    return f"{v:,f}" if v == v.to_integral() else f"{v:,}"


def _d(v):
    return v.strftime("%d/%m/%Y") if v else ""


class Box(Flowable):
    """Small checkbox, ticked or empty."""
    def __init__(self, done=False, size=3.2 * mm):
        super().__init__()
        self.done, self.size = done, size
        self.width = self.height = size

    def draw(self):
        c, s = self.canv, self.size
        c.setStrokeColor(DARK)
        c.setLineWidth(0.7)
        c.rect(0, 0, s, s)
        if self.done:
            c.setLineWidth(1.1)
            c.line(s * 0.18, s * 0.5, s * 0.42, s * 0.2)
            c.line(s * 0.42, s * 0.2, s * 0.85, s * 0.85)


def _page(canvas, doc, wo):
    canvas.saveState()
    w, h = A4
    if not wo.number:
        canvas.setFont("Helvetica-Bold", 90)
        canvas.setFillColor(colors.Color(0.93, 0.11, 0.14, alpha=0.10))
        canvas.translate(w / 2, h / 2)
        canvas.rotate(40)
        canvas.drawCentredString(0, 0, "DRAFT")
        canvas.rotate(-40)
        canvas.translate(-w / 2, -h / 2)
    canvas.setStrokeColor(LINE)
    canvas.line(18 * mm, 14 * mm, w - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(GREY)
    canvas.drawString(18 * mm, 10 * mm, f"{wo.number or 'DRAFT - not approved'}  ·  PID {wo.pid}")
    canvas.drawRightString(w - 18 * mm, 10 * mm, f"{doc.page}")
    canvas.restoreState()


def render_pdf(wo):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm,
                            bottomMargin=20 * mm, title=f"Work Order {wo.number or 'DRAFT'}",
                            author="PT. Flow Force Indonesia")
    W = A4[0] - 36 * mm
    has = wo.has
    story = []

    # ---- header: logo + company left, title right
    left = []
    if os.path.exists(LOGO):
        left.append(Image(LOGO, width=62 * mm, height=62 * mm * 258 / 1200))
        left.append(Spacer(1, 2 * mm))
    left.append(P(COMPANY[0], "b"))
    left += [P(x) for x in COMPANY[1:]]
    right = [P("Work Order", "title"), Spacer(1, 1 * mm),
             P(f"Work Order# {wo.number or 'DRAFT'}", "num")]
    if wo.title:
        right.append(P(wo.title, "r"))
    head = Table([[left, right]], colWidths=[W * 0.55, W * 0.45])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 8 * mm)]

    # ---- addresses left, details right
    addr = []
    if has("invoice_address"):
        addr += [P("Invoice Address", "n"), P(wo.customer_name, "b"), P(wo.invoice_address), Spacer(1, 4 * mm)]
    elif wo.customer_name:
        addr += [P("Customer", "n"), P(wo.customer_name, "b"), Spacer(1, 4 * mm)]
    if has("delivery_address"):
        addr += [P("Delivery Address", "n"), P(wo.delivery_address)]
    kv = []
    if has("order_date"):
        kv.append(("Order Date :", _d(wo.order_date)))
    if has("required_by"):
        kv.append(("Required By :", _d(wo.required_by)))
    if has("customer_po"):
        kv.append(("Customer PO No # :", wo.customer_po))
    if has("quotation_no"):
        kv.append(("Quotation / Enquiry No :", wo.quotation_no))
    if has("sales_order"):
        if wo.sales_order_no:
            kv.append(("Sales Order # :", wo.sales_order_no))
        kv.append(("JOB No :", wo.job_no))
    kv.append(("PID :", wo.pid))
    if has("delivery_method"):
        kv.append(("Delivery Method :", wo.delivery_method))
    if has("team"):
        kv.append(("Team :", wo.team.name))
        kv.append(("Assigned To :", ", ".join(u.full_name or u.email for u in wo.assignees.all())))
    if wo.approved_at:
        kv.append(("Approved :", f"{_d(wo.approved_at)} · {wo.approved_by.full_name if wo.approved_by else ''}"))
    kvt = Table([[P(k, "label"), P(v, "r")] for k, v in kv], colWidths=[W * 0.24, W * 0.21])
    kvt.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                             ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    mid = Table([[addr or [P("")], kvt]], colWidths=[W * 0.55, W * 0.45])
    mid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [mid, Spacer(1, 6 * mm)]

    # ---- item table
    lines = list(wo.lines.all())
    if has("items") and lines:
        prices = has("prices")
        if prices:
            cols = [8 * mm, W - 8 * mm - 18 * mm - 14 * mm - 26 * mm - 22 * mm - 28 * mm, 18 * mm, 14 * mm, 26 * mm, 22 * mm, 28 * mm]
            data = [[P("#", "th"), P("Item & Description", "th"), P("Qty", "thr"), P("Unit", "th"), P("Rate", "thr"),
                     P("Discount", "thr"), P("Amount", "thr")]]
        else:
            cols = [8 * mm, W - 8 * mm - 22 * mm - 22 * mm, 22 * mm, 22 * mm]
            data = [[P("#", "th"), P("Item & Description", "th"), P("Qty", "thr"), P("Unit", "th")]]
        sub = Decimal(0)
        for i, ln in enumerate(lines, 1):
            row = [P(i), P(ln.description), P(_qty(ln.qty), "r"), P(ln.unit)]
            if prices:
                amt = ln.amount
                sub += amt or 0
                row += [P(_money(ln.rate), "r"), P(_money(ln.discount) if ln.discount else "", "r"), P(_money(amt), "r")]
            data.append(row)
        t = Table(data, colWidths=cols, repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), DARK), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("TOPPADDING", (0, 0), (-1, -1), 4),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        story.append(t)
        if prices:
            ppn = (sub * Decimal(wo.ppn_percent) / 100).quantize(Decimal("0.01"))
            tot = Table([[P("Sub Total", "r"), P(_money(sub), "r")],
                         [P(f"PPN {Decimal(wo.ppn_percent).normalize()}%", "r"), P(_money(ppn), "r")],
                         [P("Total", "rb"), P(f"{wo.currency}{_money(sub + ppn)}", "rb")]],
                        colWidths=[W - 40 * mm - 34 * mm, 34 * mm], hAlign="RIGHT")
            tot.setStyle(TableStyle([("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#f3f4f6")),
                                     ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            story += [Spacer(1, 2 * mm), tot]
        story.append(Spacer(1, 4 * mm))

    # ---- stage checklist
    items = list(wo.checklist.all())
    if has("stages") and items:
        story.append(P("Stage Checklist", "h"))
        rows, cur = [], None
        for it in items:
            if it.stage != cur:
                cur = it.stage
                from .models import STAGE_NAMES
                rows.append(["", P(f"{it.stage}. {STAGE_NAMES.get(it.stage, '')}", "b"), ""])
            done = f"{it.done_by.full_name if it.done_by else ''} {_d(it.done_at)}".strip() if it.done else ""
            rows.append([Box(it.done), P(it.text), P(done, "small")])
        ct = Table(rows, colWidths=[7 * mm, W - 7 * mm - 45 * mm, 45 * mm])
        ct.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (0, -1), 1)]))
        story.append(ct)

    if has("instructions") and wo.instructions.strip():
        story += [P("Scope & Instructions", "h"), P(wo.instructions)]
    if has("notes") and wo.notes.strip():
        story += [P("Notes", "h"), P(wo.notes)]

    # ---- images (two per row)
    if has("images"):
        imgs = [a for a in wo.attachments.all() if a.include_in_pdf and a.is_image]
        cells = []
        for a in imgs:
            try:
                from PIL import Image as PILImage
                with a.file.open("rb") as fh:
                    raw = fh.read()
                pim = PILImage.open(io.BytesIO(raw))
                pim.load()
                if pim.mode not in ("RGB", "L"):
                    pim = pim.convert("RGB")
                pim.thumbnail((1400, 1400))
                out = io.BytesIO()
                pim.save(out, "JPEG", quality=85)
                out.seek(0)
                iw, ih = pim.size
                maxw, maxh = W / 2 - 6 * mm, 80 * mm
                scale = min(maxw / iw, maxh / ih)
                cell = [Image(out, width=iw * scale, height=ih * scale)]
                if a.caption:
                    cell.append(P(a.caption, "small"))
                cells.append(cell)
            except Exception:
                continue
        if cells:
            story.append(P("Images", "h"))
            rows = [cells[i:i + 2] + ([""] if len(cells[i:i + 2]) == 1 else []) for i in range(0, len(cells), 2)]
            it = Table(rows, colWidths=[W / 2, W / 2])
            it.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            story.append(it)

    if has("terms") and wo.terms.strip():
        story += [P("Terms & Conditions", "h"), P(wo.terms, "small")]

    if has("signatures"):
        def sig(label, name="", when=""):
            return [P(label, "small"), Spacer(1, 16 * mm), P(name or "&nbsp;", "b") if name else P(""),
                    P(when, "small")]
        names = ", ".join(u.full_name or u.email for u in wo.assignees.all())
        st = Table([[sig("Prepared by", wo.created_by.full_name if wo.created_by else ""),
                     sig("Approved by", wo.approved_by.full_name if wo.approved_by else "", _d(wo.approved_at)),
                     sig("Received by", names)]], colWidths=[W / 3] * 3)
        st.setStyle(TableStyle([("BOX", (0, 0), (0, 0), 0.5, LINE), ("BOX", (1, 0), (1, 0), 0.5, LINE),
                                ("BOX", (2, 0), (2, 0), 0.5, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        story += [Spacer(1, 8 * mm), KeepTogether([st])]

    doc.build(story, onFirstPage=lambda c, d: _page(c, d, wo), onLaterPages=lambda c, d: _page(c, d, wo))
    return buf.getvalue()
