"""Single work order PDF (landscape): page 1 = job, workflow, departments; page 2 = specification + checklist.

Names only (with designation), no signature lines and no blank spaces that could be filled in later.
"""
import io
import os
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (CondPageBreak, Flowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from . import project_defaults as D
from .models import WOItem, WorkOrder
from .project_services import dept_label, person

LOGO = os.path.join(os.path.dirname(__file__), "assets", "flowforce_logo.png")
COMPANY = "PT. Flow Force Indonesia"
ADDRESS = "Kawasan Industri Delta Silicon 5, Jl. Kenari 1 Blok G1 No.23D, Cikarang 17550, Indonesia"
DARK = colors.HexColor("#3c3c3c")
GREY = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d1d5db")
SOFT = colors.HexColor("#f3f4f6")
HEAD = colors.HexColor("#6b7280")
PAGE_W = landscape(A4)[0]
W = PAGE_W - 24 * mm


def _st(name, **kw):
    base = dict(fontName="Helvetica", fontSize=8, leading=10)
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "n": _st("n"), "b": _st("b", fontName="Helvetica-Bold"), "s": _st("s", fontSize=7, leading=8.5, textColor=GREY),
    "title": _st("t", fontSize=20, leading=22, alignment=TA_RIGHT),
    "num": _st("num", fontName="Helvetica-Bold", fontSize=10, leading=12, alignment=TA_RIGHT),
    "h": _st("h", fontName="Helvetica-Bold", fontSize=9, leading=11, textColor=colors.white),
    "th": _st("th", fontSize=7.5, leading=9, textColor=colors.white),
    "thc": _st("thc", fontSize=7.5, leading=9, textColor=colors.white, alignment=TA_CENTER),
    "c": _st("c", alignment=TA_CENTER), "cs": _st("cs", fontSize=6.5, leading=8, alignment=TA_CENTER, textColor=GREY),
    "lbl": _st("lbl", fontSize=7, leading=8.5, textColor=GREY),
}


def P(text, style="n"):
    return Paragraph(text, S[style])


def e(v):
    return escape(str(v or "")).replace("\n", "<br/>")


def d(v):
    return v.strftime("%d/%m/%Y") if v else "-"


class Box(Flowable):
    def __init__(self, done=False, na=False, size=3.0 * mm):
        super().__init__()
        self.done, self.na, self.size = done, na, size
        self.width = self.height = size

    def draw(self):
        c, s = self.canv, self.size
        c.setStrokeColor(GREY if self.na else DARK)
        c.setLineWidth(0.7)
        c.rect(0, 0, s, s)
        if self.done:
            c.setLineWidth(1.1)
            c.line(s * .18, s * .5, s * .42, s * .2)
            c.line(s * .42, s * .2, s * .85, s * .85)
        elif self.na:
            c.setLineWidth(0.8)
            c.line(s * .2, s * .5, s * .8, s * .5)


class SpecBreak(CondPageBreak):
    """Page 2 starts on a new page; if page 1 ran over, it follows on the same page when there is room."""
    def __init__(self):
        super().__init__(110 * mm)

    def wrap(self, aw, ah):
        if self.canv.getPageNumber() == 1:
            self.height = ah + 1
        return super().wrap(aw, ah)


def _tight(t, extra=()):
    t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                           ("TOPPADDING", (0, 0), (-1, -1), 0.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE")] + list(extra)))
    return t


def tick(text, done=False, w=40 * mm, na=False):
    return _tight(Table([[Box(done, na), P(text)]], colWidths=[4.2 * mm, w - 4.2 * mm]))


def section(title):
    t = Table([[P(title, "h")]], colWidths=[W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), DARK), ("TOPPADDING", (0, 0), (-1, -1), 1.8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8)]))
    return t


def _grid_style(extra=()):
    return TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                       ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                       ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)] + list(extra))


def _number(wo):
    if wo.number:
        return wo.number
    return f"DRAFT{f' · Revision {wo.revision} of {wo.parent.number}' if wo.revision and wo.parent_id else ''}"


def _logo(width):
    if os.path.exists(LOGO):
        return Image(LOGO, width=width, height=width * 258 / 1200)
    return P(f"<b>{COMPANY}</b>", "b")


# ------------------------------------------------------------------ canvas: footer, page x of y, draft watermark

def _canvas_maker(wo):
    footer_left = "  ·  ".join(x for x in [wo.number or "DRAFT", wo.pid_label if wo.is_internal else f"PID / JOB No {wo.pid}",
                                          wo.customer_name, COMPANY] if x)
    draft = not wo.number

    class C(rl_canvas.Canvas):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total):
            w, h = landscape(A4)
            self.saveState()
            if draft:
                self.setFont("Helvetica-Bold", 70)
                self.setFillColor(colors.Color(0.93, 0.11, 0.14, alpha=0.07))
                self.translate(w / 2, h / 2)
                self.rotate(25)
                self.drawCentredString(0, 0, "DRAFT")
                self.rotate(-25)
                self.translate(-w / 2, -h / 2)
            self.setStrokeColor(LINE)
            self.line(12 * mm, 7 * mm, w - 12 * mm, 7 * mm)
            self.setFont("Helvetica", 6.5)
            self.setFillColor(GREY)
            self.drawString(12 * mm, 4.5 * mm, footer_left[:150])
            self.drawRightString(w - 12 * mm, 4.5 * mm, f"Form {D.FORM_NO} · Rev. {D.FORM_REV} · Effective {D.FORM_EFFECTIVE}"
                                                     f" · Page {self._pageNumber} of {total}")
            self.restoreState()
    return C


# ------------------------------------------------------------------ page 1

def _page1(wo, depts, stages):
    story = []
    left = [_logo(52 * mm), Spacer(1, 1.5 * mm), P(f"<b>{COMPANY}</b> · {ADDRESS}", "s")]
    kind = Table([[tick(label, wo.wo_type == code, w) for (code, label), w in zip(D.TYPES, (22 * mm, 20 * mm, 20 * mm))]],
                 colWidths=[22 * mm, 20 * mm, 20 * mm], hAlign="RIGHT")
    kind.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    right = [P("WORK ORDER", "title"), P(f"WO No. {e(_number(wo))}  ·  Rev. {wo.revision}", "num"), Spacer(1, 0.8 * mm), kind]
    head = Table([[left, right]], colWidths=[W * .55, W * .45])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 1.0 * mm)]

    issue = d(timezone.localtime(wo.approved_at).date()) if wo.approved_at else "Not issued yet"
    first = ("Internal ref", wo.reference) if wo.is_internal else ("PID / JOB No", wo.pid)
    info = [
        (first[0], first[1], "Customer", wo.customer_name, "Customer PO No", wo.customer_po, "Issue date", issue),
        ("Quotation / ENQ", wo.quotation_no, "Customer contact", wo.customer_contact, "PO date", d(wo.order_date),
         "Required by", d(wo.required_by)),
        ("Sales Order", wo.sales_order_no, "Delivery method", wo.delivery_method, "Project manager",
         person(wo.project_manager), "Title", wo.title),
    ]
    rows = [[P(a, "lbl"), P(e(b) or "-", "b"), P(c, "lbl"), P(e(dd) or "-", "b"), P(f, "lbl"), P(e(g) or "-", "b"),
             P(h, "lbl"), P(e(i) or "-", "b")] for a, b, c, dd, f, g, h, i in info]
    rows.append([P("Invoice address", "lbl"), P(e(wo.invoice_address) or "-"), "", "", P("Delivery address", "lbl"),
                 P(e(wo.delivery_address) or "-"), "", ""])
    q = (W - 96 * mm) / 4
    it = Table(rows, colWidths=[24 * mm, q + 14 * mm, 22 * mm, q, 24 * mm, q, 26 * mm, q - 14 * mm])
    it.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE),
                            ("BACKGROUND", (0, 0), (0, -1), SOFT), ("BACKGROUND", (2, 0), (2, -2), SOFT),
                            ("BACKGROUND", (4, 0), (4, -1), SOFT), ("BACKGROUND", (6, 0), (6, -2), SOFT),
                            ("SPAN", (1, 3), (3, 3)), ("SPAN", (5, 3), (7, 3)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                            ("TOPPADDING", (0, 0), (-1, -1), 1.8), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8)]))
    story += [it, Spacer(1, 1.0 * mm)]

    # 1. scope
    story.append(section("1.  SCOPE OF WORK / ITEMS"))
    data = [[P("#", "thc"), P("Item & Description", "th"), P("Qty", "thc"), P("Unit", "thc")]]
    for n, ln in enumerate(wo.lines.all(), 1):
        qty = "" if ln.qty is None else f"{ln.qty:,.3f}".rstrip("0").rstrip(".")
        data.append([P(str(n), "c"), P(e(ln.description)), P(qty or "-", "c"), P(e(ln.unit) or "-", "c")])
    if len(data) == 1:
        data.append([P("-", "c"), P("-"), P("-", "c"), P("-", "c")])
    t = Table(data, colWidths=[8 * mm, W - 8 * mm - 40 * mm, 20 * mm, 20 * mm], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    story += [t, Spacer(1, 1.0 * mm)]

    # 2. workflow
    story.append(section("2.  PROJECT WORKFLOW  (ticked when the stage is completed · responsible team · target date)"))
    half = W / 2

    def stage_cell(st):
        if st is None:
            return ""
        c = Table([[Box(st.done), P(f"<b>{st.number}</b>", "b"), [P(f"<b>{e(st.name)}</b>"), P(e(st.description), "s")],
                    P(e(st.teams_label) or "-", "c"), P(d(st.target_date), "c")]],
                  colWidths=[5 * mm, 6 * mm, half - 51 * mm, 20 * mm, 20 * mm])
        c.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        return c
    rows_n = (len(stages) + 1) // 2
    rows = [[stage_cell(stages[i]), stage_cell(stages[i + rows_n] if i + rows_n < len(stages) else None)]
            for i in range(rows_n)] or [[P("-"), ""]]
    wf = Table(rows, colWidths=[half, half])
    wf.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEBELOW", (0, 0), (-1, -2), 0.3, LINE),
                            ("LINEAFTER", (0, 0), (0, -1), 0.6, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    story += [wf, Spacer(1, 1.0 * mm)]

    # 3. departments
    story.append(section("3.  DEPARTMENT RESPONSIBILITIES  (each department ticks its own tasks in the workspace)"))
    TW = W - 108 * mm
    data = [[P("Department", "th"), P("PIC & team", "th"), P("Tasks", "th"), P("Target date", "thc"), P("Status", "thc")]]
    for dp in depts:
        tasks = dp.tasks()
        cells = [tick(e(x.text), x.state == WOItem.STATE_DONE, TW / 3 - 2 * mm, x.state == WOItem.STATE_NA) for x in tasks]
        grid = [cells[i:i + 3] + [""] * (3 - len(cells[i:i + 3])) for i in range(0, len(cells), 3)] or [[P("-"), "", ""]]
        g = Table(grid, colWidths=[TW / 3] * 3)
        g.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0.5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5)]))
        who = [f"<b>{e(person(dp.pic))}</b> (PIC)" if dp.pic else '<font color="#b45309">PIC not set</font>']
        who += [e(m.full_name or m.email) for m in dp.members.all() if m.id != dp.pic_id]
        data.append([P(f"<b>{e(dept_label(dp.team).upper())}</b>"), P("<br/>".join(who)), g, P(d(dp.target_date), "c"),
                     tick("Done", bool(dp.items.all()) and dp.is_complete, 16 * mm)])
    if len(data) == 1:
        data.append([P("-"), P("-"), P("-"), P("-", "c"), P("-", "c")])
    tt = Table(data, colWidths=[32 * mm, 36 * mm, TW, 22 * mm, 18 * mm], repeatRows=1)
    tt.setStyle(_grid_style([("BACKGROUND", (0, 1), (0, -1), SOFT)]))
    story += [tt, Spacer(1, 1.0 * mm)]

    # 4. instructions & notes
    if wo.instructions or wo.notes:
        nt = Table([[[P("<b>Scope & instructions</b>"), P(e(wo.instructions) or "-")],
                     [P("<b>Notes</b>"), P(e(wo.notes) or "-")]]], colWidths=[W / 2, W / 2])
        nt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story += [KeepTogether([section("4.  INSTRUCTIONS & NOTES"), nt]), Spacer(1, 1.0 * mm)]

    # sign-off: names only
    parts = [f'<font color="#6b7280">Prepared by</font> <b>{e(person(wo.created_by))} · '
             f'{d(timezone.localtime(wo.created_at).date() if wo.created_at else None)}</b>']
    if wo.approved_by and wo.approved_at:
        parts.append(f'<font color="#6b7280">Approved by</font> <b>{e(person(wo.approved_by))} · '
                     f'{d(timezone.localtime(wo.approved_at).date())}</b>')
    else:
        parts.append('<font color="#6b7280">Status</font> <b>Waiting for GM approval</b>'
                     if wo.status == WorkOrder.STATUS_IN_REVIEW else '<font color="#6b7280">Status</font> <b>Draft</b>')
    parts.append('<font color="#6b7280">Issued to</font> <b>all departments listed in section 3</b>')
    so = Table([[P(" &nbsp;&nbsp;|&nbsp;&nbsp; ".join(parts))]], colWidths=[W])
    so.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, 0), 3)]))
    story.append(KeepTogether([so]))
    return story


# ------------------------------------------------------------------ page 2

def _page2(wo, depts):
    story = [SpecBreak()]
    head = Table([[_logo(44 * mm), [P("WORK ORDER - SPECIFICATIONS", "title"),
                                    P(f"WO No. {e(_number(wo))}  ·  Rev. {wo.revision}", "num")]]], colWidths=[W * .4, W * .6])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 1.2 * mm)]
    first = ("Internal ref", wo.reference) if wo.is_internal else ("PID / JOB No", wo.pid)
    ref = Table([[P(first[0], "lbl"), P(e(first[1]) or "-", "b"), P("Customer", "lbl"), P(e(wo.customer_name) or "-", "b"),
                  P("Customer PO No", "lbl"), P(e(wo.customer_po) or "-", "b"), P("Quotation / ENQ", "lbl"),
                  P(e(wo.quotation_no) or "-", "b")]],
                colWidths=[22 * mm, 22 * mm, 20 * mm, 52 * mm, 26 * mm, 30 * mm, 26 * mm, W - 198 * mm])
    ref.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE),
                             ("BACKGROUND", (0, 0), (0, 0), SOFT), ("BACKGROUND", (2, 0), (2, 0), SOFT),
                             ("BACKGROUND", (4, 0), (4, 0), SOFT), ("BACKGROUND", (6, 0), (6, 0), SOFT),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [ref, Spacer(1, 2 * mm)]

    pending = wo.spec_status != WorkOrder.SPEC_CONFIRMED
    story.append(section("A.  CUSTOMER REQUIREMENT vs FINAL CONFIRMED SPECIFICATION"
                         + ("   -   PENDING CONFIRMATION" if pending else "")))
    data = [[P("#", "thc"), P("Item / parameter", "th"), P("Customer requirement (enquiry / PO)", "th"),
             P("Final specification confirmed", "th"), P("Changed", "thc"), P("Reason / remarks", "th")]]
    for n, r in enumerate(wo.spec_rows.all(), 1):
        data.append([P(str(n), "c"), P(f"<b>{e(r.parameter)}</b>"), P(e(r.requirement) or "-"),
                     P(f"<b>{e(r.final)}</b>" if r.final else '<font color="#b45309">Pending</font>'), Box(r.changed),
                     P(e(r.reason) or "-", "s")])
    if len(data) == 1:
        data.append([P("-", "c"), P("-"), P("-"), P("-"), P("-", "c"), P("-", "s")])
    half = (W - 8 * mm - 34 * mm - 16 * mm - 58 * mm) / 2
    spt = Table(data, colWidths=[8 * mm, 34 * mm, half, half, 16 * mm, 58 * mm], repeatRows=1)
    spt.setStyle(_grid_style([("ALIGN", (4, 1), (4, -1), "CENTER"), ("BACKGROUND", (3, 1), (3, -1), colors.HexColor("#f0fdf4")),
                              ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2)]))
    story += [spt, Spacer(1, 2 * mm)]

    docs = list(wo.ref_docs.all())
    if docs:
        story.append(section("B.  REFERENCE DOCUMENTS"))
        dd = [[P("Document", "th"), P("Number / reference", "th"), P("Revision", "thc"), P("Date", "thc"), P("From", "th")]]
        dd += [[P(e(r.document)), P(e(r.reference) or "-", "b"), P(e(r.revision) or "-", "c"), P(d(r.doc_date), "c"),
                P(e(r.source) or "-")] for r in docs]
        dt = Table(dd, colWidths=[80 * mm, 80 * mm, 24 * mm, 26 * mm, W - 210 * mm], repeatRows=1)
        dt.setStyle(_grid_style())
        story += [dt, Spacer(1, 2 * mm)]

    story.append(section(("C" if docs else "B") + ".  QUALITY & DOCUMENTS CHECKLIST - BY DEPARTMENT  "
                         "(ticked in the workspace · N/A if not required)"))
    IW = W - 34 * mm - 44 * mm
    cell_w = IW / 3

    def item(x):
        if x.state == WOItem.STATE_DONE:
            col, ref = "#15803d", x.doc_ref or "done"
        elif x.state == WOItem.STATE_NA:
            col, ref = "#9ca3af", "N/A"
        else:
            col, ref = "#b45309", "Pending"
        t = Table([[Box(x.state == WOItem.STATE_DONE, x.state == WOItem.STATE_NA),
                    P(f'{e(x.text)} <font size="6" color="{col}">[{e(ref)}]</font>')]], colWidths=[4.6 * mm, cell_w - 6 * mm])
        return _tight(t)
    data = [[P("Department", "th"), P("PIC - name & designation", "th"), P("Checklist  (document reference in brackets)", "th")]]
    for dp in depts:
        checks = dp.checks()
        if not checks:
            continue
        cells = [item(x) for x in checks]
        grid = [cells[i:i + 3] + [""] * (3 - len(cells[i:i + 3])) for i in range(0, len(cells), 3)]
        g = Table(grid, colWidths=[cell_w] * 3)
        g.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0.3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0.3), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        data.append([P(f"<b>{e(dept_label(dp.team).upper())}</b>"), P(e(person(dp.pic)) or "-"), g])
    if len(data) == 1:
        data.append([P("-"), P("-"), P("-")])
    dt2 = Table(data, colWidths=[34 * mm, 44 * mm, IW], repeatRows=1)
    dt2.setStyle(_grid_style([("BACKGROUND", (0, 1), (0, -1), SOFT), ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2)]))
    story += [dt2, Spacer(1, 1.2 * mm)]

    story.append(section(("D" if docs else "C") + ".  SPECIFICATION CONFIRMATION"))
    rule = Table([[P("The <b>final confirmed specification</b> above is what the teams build, buy and check against. "
                     "Any change after confirmation is made as a <b>work order revision</b> (Rev. 1, Rev. 2 ...) and "
                     "approved again.")]], colWidths=[W])
    rule.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbeb")),
                              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    if pending:
        parts = ['<font color="#b45309"><b>Specification pending confirmation</b></font> - the teams are informed in the '
                 'workspace when it is confirmed']
    else:
        cust = " · ".join(x for x in [e(wo.spec_customer_by), d(wo.spec_customer_date), e(wo.spec_customer_method)] if x and x != "-")
        parts = [f'<font color="#6b7280">Confirmed by customer</font> <b>{cust}</b>',
                 f'<font color="#6b7280">Confirmed by Flow Force</font> <b>{e(person(wo.spec_confirmed_by))} · '
                 f'{d(timezone.localtime(wo.spec_confirmed_at).date() if wo.spec_confirmed_at else None)}</b>']
    if wo.approved_by and wo.approved_at:
        parts.append(f'<font color="#6b7280">Approved by</font> <b>{e(person(wo.approved_by))} · '
                     f'{d(timezone.localtime(wo.approved_at).date())}</b>')
    conf = Table([[P(" &nbsp;&nbsp;|&nbsp;&nbsp; ".join(parts))]], colWidths=[W])
    conf.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 4)]))
    story += [KeepTogether([rule, conf])]
    return story


def render_project_pdf(wo):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=6 * mm,
                            bottomMargin=9 * mm, title=f"Work Order {wo.number or 'DRAFT'}", author=COMPANY)
    depts = list(wo.departments.select_related("team", "pic").prefetch_related("members", "items"))
    stages = list(wo.wo_stages.all())
    story = _page1(wo, depts, stages) + _page2(wo, depts)
    doc.build(story, canvasmaker=_canvas_maker(wo))
    return buf.getvalue()
