import io
import json
from unittest.mock import patch

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from auth_app.models import EmployeeUser
from employee_management.models import Department
from tasks.models import EmailLog, Notification

from .models import MasterSheet, QuoteRevision, Vendor, VendorMasterFile, ZohoConnection
from .services import sheet as sheet_svc
from .services import workflow, zoho
from .services.excel_io import append_to_master, import_master


def bos_like_workbook():
    """Small workbook shaped like the BOS master (Sheet106 style + an old sheet without ENQ)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A2"] = "Line"
    ws["B2"] = "JENCO Quote No."
    ws["E4"] = 1035
    ws["F4"] = "=E4*0.7"
    ws2 = wb.create_sheet("Sheet106")
    ws2["A1"] = "Quotation No. 20263557"
    ws2["A2"] = "Enquiry No. ENQ/2026/10/484"
    ws2["C5"] = "BOS Quote No.\xa0"
    ws2["C8"] = "20263557"
    ws2["D8"] = "Z115313"
    ws2["G8"] = 6
    ws2["I8"] = "=G8*(1-0.15)"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def priced_snapshot(lines):
    """Standard template with formula results filled in, like the browser saves it."""
    snap = sheet_svc.build_standard_snapshot(
        enquiry_no="ENQ/2026/11/1", customer="PT. Test", vendor_name="BOS", vendor_quote_no="123",
        currency="EUR", exchange_rate=22000, margin_factor=0.8, duty_factor=0.9, freight_factor=1,
        discount=0.15, lines=lines)
    sh = sheet_svc.first_sheet(snap)
    for i, ln in enumerate(lines):
        r = sheet_svc.FIRST_LINE_ROW + i
        sh["cellData"][str(r)]["12"]["v"] = ln["sell"]
    return snap


class SheetServiceTests(TestCase):
    def test_standard_template_formulas(self):
        snap = sheet_svc.build_standard_snapshot(
            enquiry_no="ENQ/2026/10/484", customer="PT. Indofood", vendor_name="BOS", vendor_quote_no="20263557",
            currency="EUR", exchange_rate=22000, margin_factor=0.8, duty_factor=0.9, freight_factor=1, discount=0.15)
        sh = sheet_svc.first_sheet(snap)
        self.assertEqual(sheet_svc.get_cell(sh, 0, 2)["v"], "ENQ/2026/10/484")
        self.assertEqual(sheet_svc.get_cell(sh, 0, 9)["v"], 22000.0)
        row9 = sheet_svc.get_cell(sh, 8, 12)
        self.assertIn("ROUND((K9+L9)/D9,-3)", row9["f"])
        self.assertEqual(sheet_svc.get_cell(sh, 8, 9)["f"], '=IF(I9="","",I9/$J$2/$J$3/$J$4)')

    def test_extract_lines_reads_computed_prices(self):
        snap = priced_snapshot([
            {"part": "Z115313", "description": "SPRING", "qty": 6, "unit_price": 6, "sell": 163000},
            {"part": "18210005", "description": "STUFFING BOX", "qty": 3, "unit_price": 545.5, "sell": 14168000},
        ])
        lines, problems = sheet_svc.extract_lines(snap)
        self.assertEqual(problems, [])
        self.assertEqual([(l["part"], l["qty"], l["rate"]) for l in lines],
                         [("Z115313", 6.0, 163000.0), ("18210005", 3.0, 14168000.0)])

    def test_extract_lines_flags_uncalculated(self):
        snap = sheet_svc.build_standard_snapshot(
            enquiry_no="E", customer="C", vendor_name="V", vendor_quote_no="", currency="EUR", exchange_rate=1,
            margin_factor=1, duty_factor=1, freight_factor=1, discount=0,
            lines=[{"part": "A", "description": "x", "qty": 1, "unit_price": 10}])
        lines, problems = sheet_svc.extract_lines(snap)
        self.assertEqual(lines, [])
        self.assertIn("save it once", problems[0])

    def test_diff_snapshots(self):
        a = priced_snapshot([{"part": "A", "description": "x", "qty": 1, "unit_price": 10, "sell": 100}])
        b = json.loads(json.dumps(a))
        sh = sheet_svc.first_sheet(b)
        sh["cellData"]["0"]["9"]["v"] = 21000
        changes = sheet_svc.diff_snapshots(a, b)
        self.assertEqual(changes, [("Price Table", "J1", "22000.0", "21000")])


class ExcelImportTests(TestCase):
    def test_import_master_reads_details_and_formulas(self):
        sheets = import_master(bos_like_workbook())
        self.assertEqual([s["name"] for s in sheets], ["Sheet1", "Sheet106"])
        old, new = sheets
        self.assertEqual(old["details"]["enquiry_no"], "")
        self.assertEqual(new["details"]["enquiry_no"], "ENQ/2026/10/484")
        self.assertEqual(new["details"]["quotation_no"], "20263557")
        self.assertEqual(new["details"]["vendor_quote_no"], "20263557")
        sh = sheet_svc.first_sheet(new["snapshot"])
        self.assertEqual(sheet_svc.get_cell(sh, 7, 8)["f"], "=G8*(1-0.15)")

    def test_append_to_master_keeps_formulas(self):
        snap = priced_snapshot([{"part": "A", "description": "x", "qty": 2, "unit_price": 10, "sell": 300000}])
        data, title, count = append_to_master(bos_like_workbook().getvalue(), snap, "ENQ 2026 11 1")
        self.assertEqual(count, 3)
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(wb.sheetnames, ["Sheet1", "Sheet106", "ENQ 2026 11 1"])
        ws = wb["ENQ 2026 11 1"]
        self.assertTrue(str(ws["M9"].value).startswith("=IF(OR(K9"))
        self.assertEqual(ws["B9"].value, "A")
        # same title again gets a unique name
        _data2, title2, _count2 = append_to_master(data, snap, "ENQ 2026 11 1")
        self.assertEqual(title2, "ENQ 2026 11 1 (2)")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="")
class WorkflowTests(TestCase):
    def setUp(self):
        dept = Department.objects.create(name="VQ Test Sales", slug="vq-test-sales")
        self.gm = EmployeeUser.objects.create_user(email="gm@example.com", password="x", role="SUPER_ADMIN",
                                                   department=dept, full_name="Umapathi", status="APPROVED")
        self.ibrahim = EmployeeUser.objects.create_user(email="ibrahim@example.com", password="x", role="ADMIN",
                                                        department=dept, full_name="Md Ibrahim", status="APPROVED")
        self.employee = EmployeeUser.objects.create_user(email="emp@example.com", password="x", role="EMPLOYEE",
                                                         department=dept, full_name="Emp", status="APPROVED")
        self.vendor = Vendor.objects.create(name="BOS Homogenisers", code="BOS", currency="EUR",
                                            default_exchange_rate=22000, default_discount=0.15)
        workflow.import_vendor_master(self.vendor, SimpleUploadedFile("Price_List_BOS.xlsx", bos_like_workbook().getvalue()), self.gm)

    def _quote(self):
        quote, rev = workflow.create_quote(enquiry_number="enq/2026/11/500", customer_name="PT. Indofood",
                                           vendor=self.vendor, vendor_quote_no="20263557", quote_file=None,
                                           user=self.ibrahim)
        workflow.save_snapshot(rev, priced_snapshot([
            {"part": "Z115313", "description": "SPRING", "qty": 6, "unit_price": 6, "sell": 163000}]), self.ibrahim)
        rev.refresh_from_db()
        return quote, rev

    def test_master_import_creates_past_sheets(self):
        self.assertEqual(self.vendor.master_sheets.count(), 2)
        self.assertEqual(self.vendor.master_sheets.filter(details_missing=True).count(), 1)

    def test_full_approval_flow_without_zoho(self):
        quote, rev = self._quote()
        self.assertEqual(quote.enquiry.number, "ENQ/2026/11/500")
        self.assertTrue(rev.changes.exists())
        workflow.submit(rev, self.ibrahim)
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_IN_REVIEW)
        self.assertTrue(Notification.objects.filter(user=self.gm, link__contains=f"/vendor-quotes/quotes/{quote.id}/").exists())
        self.assertTrue(EmailLog.objects.filter(recipient_email="gm@example.com", email_type="VENDOR_QUOTE_MAIL").exists())
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(rev, self.gm, "ok")
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_APPROVED)
        self.assertEqual(rev.excel_status, QuoteRevision.OUTPUT_DONE, rev.excel_error)
        self.assertEqual(rev.master_sheet_name, "ENQ 2026 11 500")
        self.assertEqual(rev.zoho_status, QuoteRevision.OUTPUT_WAITING)
        new_master = self.vendor.current_master
        wb = openpyxl.load_workbook(new_master.file.path)
        self.assertIn("ENQ 2026 11 500", wb.sheetnames)
        self.assertEqual(VendorMasterFile.objects.filter(vendor=self.vendor).count(), 2)

    def test_employee_cannot_approve_or_edit(self):
        _quote, rev = self._quote()
        workflow.submit(rev, self.ibrahim)
        with self.assertRaises(workflow.WorkflowError):
            workflow.approve(rev, self.ibrahim)   # ADMIN is not an approver by default
        self.assertFalse(workflow.can_edit(self.employee))
        with self.assertRaises(workflow.WorkflowError):
            workflow.save_snapshot(rev, rev.snapshot, self.ibrahim)  # locked while in review

    def test_send_back_requires_comment_and_reopens(self):
        _quote, rev = self._quote()
        workflow.submit(rev, self.ibrahim)
        with self.assertRaises(workflow.WorkflowError):
            workflow.send_back(rev, self.gm, "")
        workflow.send_back(rev, self.gm, "Margin 0.85 please")
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_SENT_BACK)
        self.assertTrue(workflow.can_edit_revision(self.ibrahim, rev))

    @patch("vendor_quotes.services.zoho.create_draft_estimate")
    @patch("vendor_quotes.services.zoho.resolve_customer", return_value="cust1")
    def test_zoho_numbering_for_original_and_revision(self, _resolve, create):
        ZohoConnection.objects.create(pk=1, refresh_token="r")
        create.side_effect = [("est1", "FFI_26000278"), ("est2", "FFI_26000278R1")]
        quote, rev = self._quote()
        workflow.submit(rev, self.ibrahim)
        with self.settings(ZOHO_CLIENT_ID="id", ZOHO_CLIENT_SECRET="s"), self.captureOnCommitCallbacks(execute=True):
            workflow.approve(rev, self.gm)
        rev.refresh_from_db()
        self.assertEqual(rev.zoho_estimate_number, "FFI_26000278")
        self.assertIsNone(create.call_args_list[0].kwargs["estimate_number"])
        self.assertEqual(create.call_args_list[0].kwargs["reference"], "ENQ/2026/11/500")
        self.assertEqual(create.call_args_list[0].kwargs["lines"][0]["rate"], 163000.0)

        r1 = workflow.revise(quote, self.ibrahim)
        self.assertEqual(r1.number, 1)
        workflow.save_snapshot(r1, priced_snapshot([
            {"part": "Z115313", "description": "SPRING", "qty": 6, "unit_price": 6, "sell": 150000}]), self.ibrahim)
        workflow.submit(r1, self.ibrahim)
        with self.settings(ZOHO_CLIENT_ID="id", ZOHO_CLIENT_SECRET="s"), self.captureOnCommitCallbacks(execute=True):
            workflow.approve(r1, self.gm)
        r1.refresh_from_db()
        self.assertEqual(create.call_args_list[1].kwargs["estimate_number"], "FFI_26000278R1")
        self.assertEqual(r1.master_sheet_name, "ENQ 2026 11 500 - R1")

    def test_revision_number_helper(self):
        self.assertEqual(zoho.revision_number_for("26000231", 1), "26000231R1")
        self.assertEqual(zoho.revision_number_for("FFI_26000278", 2), "FFI_26000278R2")
        self.assertEqual(zoho.revision_number_for("26000231R5", 6), "26000231R6")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="")
class ViewTests(TestCase):
    def setUp(self):
        dept = Department.objects.create(name="VQ Test Ops", slug="vq-test-ops")
        self.gm = EmployeeUser.objects.create_user(email="gm@example.com", password="x", role="SUPER_ADMIN",
                                                   department=dept, full_name="GM", status="APPROVED")
        self.emp = EmployeeUser.objects.create_user(email="e@example.com", password="x", role="EMPLOYEE",
                                                    department=dept, full_name="E", status="APPROVED")
        self.vendor = Vendor.objects.create(name="BOS", code="BOS", currency="EUR", default_exchange_rate=22000)

    def test_pages_and_create_flow(self):
        self.client.force_login(self.gm)
        for name in ["quote_list", "quote_create", "vendor_list", "zoho_settings"]:
            self.assertEqual(self.client.get(reverse(f"vendor_quotes:{name}")).status_code, 200, name)
        resp = self.client.post(reverse("vendor_quotes:quote_create"), {
            "enquiry_number": "ENQ/2026/11/9", "customer_name": "PT. A", "vendor": self.vendor.id,
            "vendor_quote_no": "1", "start": "standard",
            "quote_file": SimpleUploadedFile("q.pdf", b"%PDF-1.4 test", content_type="application/pdf")})
        self.assertEqual(resp.status_code, 302)
        quote = self.vendor.quotes.get()
        page = self.client.get(reverse("vendor_quotes:quote_detail", args=[quote.id]))
        self.assertContains(page, "sheet-data")
        self.assertContains(page, "Send for approval")
        rev = quote.revisions.get()
        save = self.client.post(reverse("vendor_quotes:revision_save", args=[rev.id]),
                                data=json.dumps({"snapshot": priced_snapshot([{"part": "A", "description": "d", "qty": 1, "unit_price": 1, "sell": 5000}])}),
                                content_type="application/json")
        self.assertEqual(save.json()["lines"], 1)
        self.assertEqual(self.client.get(reverse("vendor_quotes:quote_file", args=[quote.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("vendor_quotes:revision_excel", args=[rev.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("vendor_quotes:vendor_detail", args=[self.vendor.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("vendor_quotes:enquiry_detail", args=[quote.enquiry.id])).status_code, 200)

    def test_employee_is_blocked(self):
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(reverse("vendor_quotes:quote_list")).status_code, 403)
        self.assertNotContains(self.client.get("/employee-dashboard/"), "Vendor Quotes")
