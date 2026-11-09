import io
import json
from unittest.mock import MagicMock, patch

import openpyxl
from django.contrib.auth.models import Group
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from auth_app.models import EmployeeUser
from employee_management.models import Department, EmployeeActivityLog
from tasks.models import EmailLog, Notification

from .models import (Enquiry, MasterSheet, QuoteChange, QuoteEvent, QuoteRevision, Vendor,
                     VendorMasterFile, VendorQuote, ZohoConnection)
from .services import sheet as sheet_svc
from .services import workflow, zoho
from .services.ai_extract import (AIExtractError, excel_to_text, extract_quote,
                                  validate_and_clean_data)
from .services.excel_io import append_to_master, import_master
from .tasks import read_vendor_quote_with_ai


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


class AIQuoteExtractionTests(TestCase):
    def setUp(self):
        dept = Department.objects.create(name="VQ AI Sales", slug="vq-ai-sales")
        self.gm = EmployeeUser.objects.create_user(
            email="gm_ai@example.com", password="x", role="SUPER_ADMIN",
            department=dept, full_name="GM AI", status="APPROVED"
        )
        self.ibrahim = EmployeeUser.objects.create_user(
            email="ibrahim_ai@example.com", password="x", role="ADMIN",
            department=dept, full_name="Md Ibrahim AI", status="APPROVED"
        )
        self.emp = EmployeeUser.objects.create_user(
            email="emp_ai@example.com", password="x", role="EMPLOYEE",
            department=dept, full_name="Emp AI", status="APPROVED"
        )
        self.vendor = Vendor.objects.create(
            name="Alpha Supplies", code="ALPHA", currency="EUR",
            default_exchange_rate=20000, default_margin_factor=0.85,
            default_duty_factor=0.9, default_freight_factor=1.05,
            default_discount=0.1
        )

    @override_settings(GEMINI_API_KEY="")
    def test_missing_api_key_raises_error(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AIExtractError) as cm:
                extract_quote(b"%PDF-dummy", "quote.pdf")
            self.assertIn("API key is not configured", str(cm.exception))

    @override_settings(GEMINI_API_KEY="dummy-key")
    def test_file_size_exceeded_raises_error(self):
        huge_bytes = b"0" * (20 * 1024 * 1024 + 1)
        with self.assertRaises(AIExtractError) as cm:
            extract_quote(huge_bytes, "quote.pdf")
        self.assertIn("exceeds the 20 MB limit", str(cm.exception))

    @override_settings(GEMINI_API_KEY="dummy-key")
    def test_unsupported_file_extension(self):
        with self.assertRaises(AIExtractError) as cm:
            extract_quote(b"word document content", "quote.docx")
        self.assertIn("Unsupported file format", str(cm.exception))

    def test_excel_to_text_conversion(self):
        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "Items"
        ws1.append(["Part No", "Description", "Qty", "Price"])
        ws1.append(["P-001", "Sensor Unit", 2, 150.5])
        ws2 = wb.create_sheet(title="Terms")
        ws2.append(["Freight", 50])
        buf = io.BytesIO()
        wb.save(buf)
        excel_bytes = buf.getvalue()

        text = excel_to_text(excel_bytes, "test.xlsx")
        self.assertIn("--- Sheet: Items ---", text)
        self.assertIn("P-001", text)
        self.assertIn("Sensor Unit", text)
        self.assertIn("--- Sheet: Terms ---", text)
        self.assertIn("Freight", text)

    def test_validation_and_clean_data(self):
        raw_data = {
            "vendor_quote_no": "VQ-100",
            "currency": "usd",
            "freight_or_courier": "75.5",
            "quote_total": "1200",
            "lines": [
                {"line_no": 1, "part_no": "A1", "description": "Good part", "qty": 10, "unit": "pcs", "unit_price": 50, "discount_percent": 15},
                {"line_no": 2, "part_no": "A2", "description": "Zero qty", "qty": 0, "unit": "pcs", "unit_price": 20},
                {"line_no": 3, "part_no": "A3", "description": "Negative price", "qty": 2, "unit": "pcs", "unit_price": -10},
                {"line_no": 4, "part_no": "", "description": "", "qty": 1, "unit": "pcs", "unit_price": 30},
                {"line_no": 5, "part_no": "A5", "description": "Over discount", "qty": 1, "unit": "pcs", "unit_price": 100, "discount_percent": 150},
            ],
            "warnings": ["Initial prompt warning"],
        }
        cleaned = validate_and_clean_data(raw_data)
        self.assertEqual(cleaned["currency"], "USD")
        self.assertEqual(cleaned["freight_or_courier"], 75.5)
        self.assertEqual(cleaned["quote_total"], 1200.0)
        self.assertEqual(len(cleaned["lines"]), 2)
        self.assertEqual(cleaned["lines"][0]["part_no"], "A1")
        self.assertEqual(cleaned["lines"][1]["part_no"], "A5")
        self.assertIsNone(cleaned["lines"][1]["discount_percent"])

        warning_texts = " ".join(cleaned["warnings"])
        self.assertIn("invalid quantity", warning_texts)
        self.assertIn("invalid unit price", warning_texts)
        self.assertIn("both part number and description are empty", warning_texts)
        self.assertIn("out of bounds", warning_texts)

    @override_settings(GEMINI_API_KEY="dummy-key")
    @patch("google.genai.Client")
    def test_extract_quote_pdf_mocked(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_response = MagicMock()
        mock_response.parsed = None
        mock_response.text = json.dumps({
            "vendor_quote_no": "VQ-PDF-999",
            "quote_date": "2026-11-09",
            "currency": "EUR",
            "freight_or_courier": 120.0,
            "quote_total": 2500.0,
            "lines": [
                {"line_no": 1, "part_no": "PART-1", "description": "Hydraulic Valve", "qty": 4, "unit": "pcs", "unit_price": 350.0, "discount_percent": 10.0, "notes": "standard"},
                {"line_no": 2, "part_no": "PART-2", "description": "Seal Kit", "qty": 10, "unit": "set", "unit_price": 45.0, "discount_percent": None, "notes": ""},
            ],
            "warnings": [],
        })
        mock_client.models.generate_content.return_value = mock_response

        res = extract_quote(b"%PDF-1.4 test binary stream", "vendor_quote.pdf")
        self.assertEqual(res["vendor_quote_no"], "VQ-PDF-999")
        self.assertEqual(res["currency"], "EUR")
        self.assertEqual(res["freight_or_courier"], 120.0)
        self.assertEqual(res["quote_total"], 2500.0)
        self.assertEqual(len(res["lines"]), 2)
        self.assertEqual(res["lines"][0]["part_no"], "PART-1")

    @override_settings(GEMINI_API_KEY="dummy-key")
    @patch("google.genai.Client")
    def test_extract_quote_excel_mocked(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_response = MagicMock()
        mock_response.parsed = None
        mock_response.text = json.dumps({
            "vendor_quote_no": "VQ-XLSX-123",
            "quote_date": "2026-11-09",
            "currency": "USD",
            "freight_or_courier": 0,
            "quote_total": 500.0,
            "lines": [
                {"line_no": 1, "part_no": "E-1", "description": "Filter", "qty": 2, "unit": "pcs", "unit_price": 250.0, "discount_percent": 5.0, "notes": ""},
            ],
            "warnings": [],
        })
        mock_client.models.generate_content.return_value = mock_response

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Part", "Price"])
        ws.append(["E-1", 250])
        buf = io.BytesIO()
        wb.save(buf)

        res = extract_quote(buf.getvalue(), "quote.xlsx")
        self.assertEqual(res["vendor_quote_no"], "VQ-XLSX-123")
        self.assertEqual(res["currency"], "USD")
        self.assertEqual(len(res["lines"]), 1)

    def test_apply_ai_result_to_sheet_snapshot(self):
        quote, rev = workflow.create_quote(
            enquiry_number="ENQ/2026/11/700", customer_name="PT. Testing",
            vendor=self.vendor, vendor_quote_no="", quote_file=None,
            user=self.ibrahim
        )
        ai_data = {
            "vendor_quote_no": "EXTRACTED-777",
            "currency": "EUR",
            "freight_or_courier": 250.0,
            "quote_total": 4500.0,
            "lines": [
                {"line_no": 1, "part_no": "P-100", "description": "Pump Model A", "qty": 2, "unit": "pcs", "unit_price": 1000.0, "discount_percent": 20.0},
                {"line_no": 2, "part_no": "P-200", "description": "Pump Model B", "qty": 3, "unit": "pcs", "unit_price": 800.0, "discount_percent": None},
            ],
            "warnings": ["Skipped unpriced spare"],
        }
        n_lines = workflow.apply_ai_result(quote, rev, ai_data, self.ibrahim)
        self.assertEqual(n_lines, 2)
        quote.refresh_from_db()
        rev.refresh_from_db()

        self.assertEqual(quote.vendor_quote_no, "EXTRACTED-777")
        self.assertEqual(quote.currency, "EUR")

        sh = sheet_svc.first_sheet(rev.snapshot)
        self.assertEqual(sheet_svc.get_cell(sh, 0, 9)["v"], 20000.0)
        self.assertEqual(sheet_svc.get_cell(sh, 1, 9)["v"], 0.85)
        self.assertEqual(sheet_svc.get_cell(sh, 2, 9)["v"], 0.9)
        self.assertEqual(sheet_svc.get_cell(sh, 3, 9)["v"], 1.05)
        self.assertEqual(sheet_svc.get_cell(sh, 4, 9)["v"], 250.0)
        self.assertEqual(sheet_svc.get_cell(sh, 5, 9)["v"], 4500.0)

        # Line 1 (Excel row 9, 0-based row 8)
        self.assertEqual(sheet_svc.get_cell(sh, 8, 1)["v"], "P-100")
        self.assertEqual(sheet_svc.get_cell(sh, 8, 2)["v"], "Pump Model A")
        self.assertEqual(sheet_svc.get_cell(sh, 8, 3)["v"], 2.0)
        self.assertEqual(sheet_svc.get_cell(sh, 8, 5)["v"], 1000.0)
        self.assertEqual(sheet_svc.get_cell(sh, 8, 6)["v"], 0.20)

        # Line 2 (Excel row 10, 0-based row 9)
        self.assertEqual(sheet_svc.get_cell(sh, 9, 1)["v"], "P-200")
        self.assertEqual(sheet_svc.get_cell(sh, 9, 2)["v"], "Pump Model B")
        self.assertEqual(sheet_svc.get_cell(sh, 9, 3)["v"], 3.0)
        self.assertEqual(sheet_svc.get_cell(sh, 9, 5)["v"], 800.0)
        self.assertEqual(sheet_svc.get_cell(sh, 9, 6)["v"], 0.1)

        self.assertTrue(rev.changes.exists())
        self.assertTrue(quote.events.filter(action="ai_filled").exists())

    def test_apply_ai_refuses_approved_revision(self):
        quote, rev = workflow.create_quote(
            enquiry_number="ENQ/2026/11/800", customer_name="PT. Approved",
            vendor=self.vendor, vendor_quote_no="", quote_file=None,
            user=self.ibrahim
        )
        workflow.save_snapshot(rev, priced_snapshot([
            {"part": "Z1", "description": "D1", "qty": 1, "unit_price": 10, "sell": 1000}
        ]), self.ibrahim)
        rev.refresh_from_db()
        workflow.submit(rev, self.ibrahim)
        rev.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(rev, self.gm)
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_APPROVED)

        # Direct workflow call raises WorkflowError
        with self.assertRaises(workflow.WorkflowError):
            workflow.apply_ai_result(quote, rev, {"lines": []}, self.ibrahim)

        # Celery task fails gracefully
        quote.quote_file = SimpleUploadedFile("quote.pdf", b"%PDF-1.4 sample")
        quote.save()
        read_vendor_quote_with_ai(quote.id, self.ibrahim.id)
        quote.refresh_from_db()
        self.assertEqual(quote.ai_status, VendorQuote.AI_FAILED)
        self.assertIn("not editable", quote.ai_error)

    @override_settings(GEMINI_API_KEY="dummy-key")
    @patch("vendor_quotes.tasks.read_vendor_quote_with_ai.delay")
    def test_ai_read_and_status_views(self, mock_task_delay):
        quote, rev = workflow.create_quote(
            enquiry_number="ENQ/2026/11/900", customer_name="PT. ViewTest",
            vendor=self.vendor, vendor_quote_no="",
            quote_file=SimpleUploadedFile("vendor.pdf", b"%PDF-dummy-content"),
            user=self.ibrahim
        )

        # Block unauthorized employee
        self.client.force_login(self.emp)
        resp = self.client.post(reverse("vendor_quotes:quote_ai_read", args=[quote.id]))
        self.assertEqual(resp.status_code, 403)

        # Ibrahim (authorized) triggers AI read
        self.client.force_login(self.ibrahim)
        resp = self.client.post(reverse("vendor_quotes:quote_ai_read", args=[quote.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["ok"], True)
        self.assertEqual(resp.json()["status"], "RUNNING")
        mock_task_delay.assert_called_once_with(quote.id, self.ibrahim.id)

        # Check AI status view
        quote.ai_status = VendorQuote.AI_DONE
        quote.ai_result = {
            "quote_total": 1500.0,
            "lines": [{"part_no": "A", "qty": 1}],
            "warnings": ["Test warning"]
        }
        quote.save()

        status_resp = self.client.get(reverse("vendor_quotes:quote_ai_status", args=[quote.id]))
        self.assertEqual(status_resp.status_code, 200)
        data = status_resp.json()
        self.assertEqual(data["status"], "DONE")
        self.assertEqual(data["lines_count"], 1)
        self.assertEqual(data["quote_total"], 1500.0)
        self.assertEqual(data["warnings"], ["Test warning"])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="")
class DeleteAndVoidTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="VQ Del Department", slug="vq-del-dept")
        self.gm = EmployeeUser.objects.create_user(
            email="gm_del@example.com", password="x", role="SUPER_ADMIN",
            department=self.dept, full_name="GM Approver", status="APPROVED"
        )
        self.ibrahim = EmployeeUser.objects.create_user(
            email="ibrahim_del@example.com", password="x", role="ADMIN",
            department=self.dept, full_name="Ibrahim Admin", status="APPROVED"
        )
        vq_group, _ = Group.objects.get_or_create(name="Vendor Quotes")
        self.editor = EmployeeUser.objects.create_user(
            email="editor_del@example.com", password="x", role="EMPLOYEE",
            department=self.dept, full_name="Editor Staff", status="APPROVED"
        )
        self.editor.groups.add(vq_group)
        self.emp = EmployeeUser.objects.create_user(
            email="unauth_del@example.com", password="x", role="EMPLOYEE",
            department=self.dept, full_name="Unauth Staff", status="APPROVED"
        )
        self.vendor = Vendor.objects.create(
            name="Vendor Del Test", code="VDT", currency="EUR",
            default_exchange_rate=22000, default_discount=0.15
        )

    def _create_quote_with_file(self, user=None, enq_num="ENQ/2026/11/100", filename="quote.pdf"):
        user = user or self.editor
        qfile = SimpleUploadedFile(filename, b"%PDF-1.4 dummy quote bytes")
        quote, rev = workflow.create_quote(
            enquiry_number=enq_num, customer_name="Customer Del",
            vendor=self.vendor, vendor_quote_no="Q-100",
            quote_file=qfile, user=user
        )
        workflow.save_snapshot(rev, priced_snapshot([
            {"part": "P1", "description": "Part 1", "qty": 2, "unit_price": 50, "sell": 150000}
        ]), user)
        rev.refresh_from_db()
        return quote, rev

    def test_rule1_delete_quote_allowed_and_cascades(self):
        quote, rev = self._create_quote_with_file(user=self.editor)
        file_path = quote.quote_file.name
        self.assertTrue(default_storage.exists(file_path))
        enq = quote.enquiry

        # Creator deletes draft quote
        self.assertTrue(workflow.can_delete_quote(quote, self.editor))
        workflow.delete_quote(quote, self.editor)

        # Quote and related records deleted
        self.assertFalse(VendorQuote.objects.filter(id=quote.id).exists())
        self.assertFalse(QuoteRevision.objects.filter(id=rev.id).exists())
        # File removed from storage
        self.assertFalse(default_storage.exists(file_path))
        # Enquiry auto-deleted since no other quotes remain
        self.assertFalse(Enquiry.objects.filter(id=enq.id).exists())
        # Activity log entry written
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="DELETE", description__icontains=enq.number
        ).exists())

    def test_rule1_delete_quote_keeps_enquiry_if_other_quotes_exist(self):
        q1, _r1 = self._create_quote_with_file(user=self.editor, enq_num="ENQ/2026/11/200", filename="q1.pdf")
        enq = q1.enquiry
        v2 = Vendor.objects.create(name="Vendor 2", code="V2", currency="EUR")
        q2, _r2 = workflow.create_quote(
            enquiry_number=enq.number, customer_name="Customer Del",
            vendor=v2, vendor_quote_no="Q-200",
            quote_file=SimpleUploadedFile("q2.pdf", b"%PDF bytes"), user=self.editor
        )

        workflow.delete_quote(q1, self.editor)
        self.assertFalse(VendorQuote.objects.filter(id=q1.id).exists())
        self.assertTrue(Enquiry.objects.filter(id=enq.id).exists())
        self.assertTrue(VendorQuote.objects.filter(id=q2.id).exists())

    def test_rule1_delete_quote_in_review_permissions(self):
        quote, rev = self._create_quote_with_file(user=self.editor)
        workflow.submit(rev, self.editor)
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_IN_REVIEW)

        # Editor (creator) cannot delete quote while in review
        self.assertFalse(workflow.can_delete_quote(quote, self.editor))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_quote(quote, self.editor)

        # Approver CAN delete quote in review
        self.assertTrue(workflow.can_delete_quote(quote, self.gm))
        workflow.delete_quote(quote, self.gm)
        self.assertFalse(VendorQuote.objects.filter(id=quote.id).exists())

    def test_rule1_delete_quote_refused_if_approved_or_void(self):
        quote, rev = self._create_quote_with_file(user=self.editor)
        workflow.submit(rev, self.editor)
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(rev, self.gm)
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_APPROVED)

        # Refused if approved
        self.assertFalse(workflow.can_delete_quote(quote, self.gm))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_quote(quote, self.gm)

        # Refused if void
        workflow.void_revision(rev, self.gm, "Voiding approved")
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_VOID)
        self.assertFalse(workflow.can_delete_quote(quote, self.gm))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_quote(quote, self.gm)

    def test_rule2_discard_revision_allowed_on_latest_unapproved(self):
        quote, r0 = self._create_quote_with_file(user=self.editor)
        workflow.submit(r0, self.editor)
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(r0, self.gm)

        r1 = workflow.revise(quote, self.editor)
        self.assertEqual(r1.number, 1)
        self.assertEqual(r1.status, QuoteRevision.STATUS_DRAFT)

        self.assertTrue(workflow.can_discard_revision(r1, self.editor))
        workflow.discard_revision(r1, self.editor)

        self.assertFalse(QuoteRevision.objects.filter(id=r1.id).exists())
        self.assertTrue(QuoteRevision.objects.filter(id=r0.id).exists())
        self.assertEqual(quote.latest_revision.id, r0.id)
        self.assertTrue(quote.events.filter(action="discard_revision").exists())
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="DELETE", description__icontains="Discarded revision R1"
        ).exists())

    def test_rule2_discard_revision_refused_on_r0_or_non_latest_or_unauthorized(self):
        quote, r0 = self._create_quote_with_file(user=self.editor)
        # Cannot discard R0
        self.assertFalse(workflow.can_discard_revision(r0, self.editor))
        with self.assertRaises(workflow.WorkflowError):
            workflow.discard_revision(r0, self.editor)

        # Disallow unauthorized employee
        with self.assertRaises(workflow.WorkflowError):
            workflow.discard_revision(r0, self.emp)

    def test_rule3_void_revision_flow(self):
        quote, rev = self._create_quote_with_file(user=self.editor)
        workflow.submit(rev, self.editor)
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(rev, self.gm)
        rev.refresh_from_db()

        # Non-approver cannot void
        self.assertFalse(workflow.can_void_revision(rev, self.editor))
        with self.assertRaises(workflow.WorkflowError):
            workflow.void_revision(rev, self.editor, "test")

        # Empty comment refused
        self.assertTrue(workflow.can_void_revision(rev, self.gm))
        with self.assertRaises(workflow.WorkflowError):
            workflow.void_revision(rev, self.gm, "   ")

        # Approver voids revision
        workflow.void_revision(rev, self.gm, "Project cancelled by customer")
        rev.refresh_from_db()
        self.assertEqual(rev.status, QuoteRevision.STATUS_VOID)
        self.assertEqual(rev.decision_comment, "Project cancelled by customer")
        self.assertEqual(rev.decided_by, self.gm)
        self.assertTrue(quote.events.filter(action="void_revision").exists())
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="STATUS_CHANGE", description__icontains="Voided revision"
        ).exists())

    def test_rule3_revise_blocked_from_void_unless_approved_exists(self):
        quote, r0 = self._create_quote_with_file(user=self.editor)
        workflow.submit(r0, self.editor)
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(r0, self.gm)
        workflow.void_revision(r0, self.gm, "Pricing obsolete")
        r0.refresh_from_db()

        # Only revision is VOID: revising blocked
        with self.assertRaises(workflow.WorkflowError):
            workflow.revise(quote, self.editor)

        # Reset: R0 approved, R1 voided -> revising clones from R0 as R2
        quote2, q2_r0 = self._create_quote_with_file(user=self.editor, enq_num="ENQ/2026/11/300")
        workflow.submit(q2_r0, self.editor)
        workflow.approve(q2_r0, self.gm)

        q2_r1 = workflow.revise(quote2, self.editor)
        workflow.save_snapshot(q2_r1, priced_snapshot([
            {"part": "P1", "description": "Part 1", "qty": 3, "unit_price": 50, "sell": 200000}
        ]), self.editor)
        workflow.submit(q2_r1, self.editor)
        workflow.approve(q2_r1, self.gm)
        workflow.void_revision(q2_r1, self.gm, "Customer rejected R1 terms")

        # Now revise quote2: should clone from last APPROVED (q2_r0) as R2
        q2_r2 = workflow.revise(quote2, self.editor)
        self.assertEqual(q2_r2.number, 2)
        self.assertEqual(q2_r2.status, QuoteRevision.STATUS_DRAFT)

    def test_rule4_delete_enquiry(self):
        quote, rev = self._create_quote_with_file(user=self.editor, enq_num="ENQ/2026/11/400")
        enq = quote.enquiry
        qfile_path = quote.quote_file.name

        # Non-approver refused
        self.assertFalse(workflow.can_delete_enquiry(self.editor, enq))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_enquiry(enq, self.editor, enq.number)

        # Mismatched confirmation number refused
        self.assertTrue(workflow.can_delete_enquiry(self.gm, enq))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_enquiry(enq, self.gm, "WRONG/NUMBER")

        # Successful delete by approver with exact number
        workflow.delete_enquiry(enq, self.gm, enq.number)
        self.assertFalse(Enquiry.objects.filter(id=enq.id).exists())
        self.assertFalse(VendorQuote.objects.filter(id=quote.id).exists())
        self.assertFalse(default_storage.exists(qfile_path))
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="DELETE", description__icontains=enq.number
        ).exists())

    def test_rule4_delete_enquiry_refused_if_approved_or_void_quote(self):
        quote, rev = self._create_quote_with_file(user=self.editor, enq_num="ENQ/2026/11/401")
        enq = quote.enquiry
        workflow.submit(rev, self.editor)
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(rev, self.gm)

        # Refused if approved
        self.assertFalse(workflow.can_delete_enquiry(self.gm, enq))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_enquiry(enq, self.gm, enq.number)

        # Refused if void
        workflow.void_revision(rev, self.gm, "Voided")
        self.assertFalse(workflow.can_delete_enquiry(self.gm, enq))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_enquiry(enq, self.gm, enq.number)

    def test_rule5_vendor_delete_and_deactivate(self):
        vendor_empty = Vendor.objects.create(name="Vendor Empty", code="VE", currency="EUR")
        mf = VendorMasterFile(vendor=vendor_empty, sheet_count=1, note="test")
        mf.file.save("test_master.xlsx", SimpleUploadedFile("test_master.xlsx", bos_like_workbook().getvalue()))
        MasterSheet.objects.create(vendor=vendor_empty, sheet_name="Sheet1", enquiry_no="ENQ/1", snapshot={})
        mf_path = mf.file.name

        # Non-admin cannot delete
        self.assertFalse(workflow.can_delete_vendor(vendor_empty, self.emp))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_vendor(vendor_empty, self.emp)

        # Approver / admin can delete empty vendor
        self.assertTrue(workflow.can_delete_vendor(vendor_empty, self.gm))
        workflow.delete_vendor(vendor_empty, self.gm)
        self.assertFalse(Vendor.objects.filter(id=vendor_empty.id).exists())
        self.assertFalse(default_storage.exists(mf_path))
        self.assertFalse(MasterSheet.objects.filter(vendor_id=vendor_empty.id).exists())
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="DELETE", description__icontains="Vendor Empty"
        ).exists())

        # Vendor with quotes cannot be deleted
        quote, _ = self._create_quote_with_file(user=self.editor)
        self.assertFalse(workflow.can_delete_vendor(self.vendor, self.gm))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_vendor(self.vendor, self.gm)

        # Deactivate / activate toggle
        workflow.toggle_vendor_active(self.vendor, self.gm)
        self.vendor.refresh_from_db()
        self.assertFalse(self.vendor.is_active)
        workflow.toggle_vendor_active(self.vendor, self.gm)
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.is_active)

    def test_rule6_vendor_master_file_delete_and_restore(self):
        vendor = Vendor.objects.create(name="Vendor Master Test", code="VMT", currency="EUR")
        mf1 = VendorMasterFile(vendor=vendor, sheet_count=1, note="v1")
        mf1.file.save("master_v1.xlsx", SimpleUploadedFile("master_v1.xlsx", bos_like_workbook().getvalue()))
        mf2 = VendorMasterFile(vendor=vendor, sheet_count=2, note="v2")
        mf2.file.save("master_v2.xlsx", SimpleUploadedFile("master_v2.xlsx", bos_like_workbook().getvalue()))

        mf1_path = mf1.file.name
        self.assertEqual(vendor.current_master.id, mf2.id)

        # Current master cannot be deleted or restored
        self.assertFalse(workflow.can_delete_master_file(mf2, self.gm))
        self.assertFalse(workflow.can_restore_master_file(mf2, self.gm))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_master_file(mf2, self.gm)
        with self.assertRaises(workflow.WorkflowError):
            workflow.restore_master_file(mf2, self.gm)

        # Non-approver cannot delete or restore
        self.assertFalse(workflow.can_delete_master_file(mf1, self.editor))
        self.assertFalse(workflow.can_restore_master_file(mf1, self.editor))

        # Restore mf1 as newest version
        restored = workflow.restore_master_file(mf1, self.gm)
        self.assertIn("Restored from", restored.note)
        vendor.refresh_from_db()
        self.assertEqual(vendor.current_master.id, restored.id)
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="UPDATE", description__icontains="Restored master file version"
        ).exists())

        # Now mf1 is older and can be deleted
        self.assertTrue(workflow.can_delete_master_file(mf1, self.gm))
        workflow.delete_master_file(mf1, self.gm)
        self.assertFalse(VendorMasterFile.objects.filter(id=mf1.id).exists())
        self.assertFalse(default_storage.exists(mf1_path))
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="DELETE", description__icontains="Deleted master file version"
        ).exists())

    def test_rule7_master_sheet_delete_and_bulk_delete(self):
        s1 = MasterSheet.objects.create(vendor=self.vendor, sheet_name="S1", enquiry_no="ENQ/1", snapshot={})
        s2 = MasterSheet.objects.create(vendor=self.vendor, sheet_name="S2", enquiry_no="ENQ/2", snapshot={})
        s3 = MasterSheet.objects.create(vendor=self.vendor, sheet_name="S3", enquiry_no="ENQ/3", snapshot={})

        # Non-editor cannot delete
        self.assertFalse(workflow.can_delete_master_sheet(s1, self.emp))
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_master_sheet(s1, self.emp)

        # Editor deletes single sheet
        self.assertTrue(workflow.can_delete_master_sheet(s1, self.editor))
        workflow.delete_master_sheet(s1, self.editor)
        self.assertFalse(MasterSheet.objects.filter(id=s1.id).exists())
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="DELETE", description__icontains="Deleted past quote sheet 'S1'"
        ).exists())

        # Editor bulk deletes sheets
        count = workflow.bulk_delete_master_sheets(self.vendor, [s2.id, s3.id], self.editor)
        self.assertEqual(count, 2)
        self.assertFalse(MasterSheet.objects.filter(id__in=[s2.id, s3.id]).exists())
        self.assertTrue(EmployeeActivityLog.objects.filter(
            activity_type="DELETE", description__icontains="Deleted 2 past quote sheet(s)"
        ).exists())

    def test_rule8_views_delete_and_void_endpoints(self):
        # 1. Quote delete view
        q, _ = self._create_quote_with_file(user=self.editor, enq_num="ENQ/2026/11/501")
        self.client.force_login(self.editor)
        resp = self.client.post(reverse("vendor_quotes:quote_delete", args=[q.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(VendorQuote.objects.filter(id=q.id).exists())

        # 2. Revision discard view
        q2, r0 = self._create_quote_with_file(user=self.editor, enq_num="ENQ/2026/11/502")
        workflow.submit(r0, self.editor)
        with self.captureOnCommitCallbacks(execute=True):
            workflow.approve(r0, self.gm)
        r1 = workflow.revise(q2, self.editor)
        resp = self.client.post(reverse("vendor_quotes:revision_discard", args=[r1.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(QuoteRevision.objects.filter(id=r1.id).exists())

        # 3. Revision void view
        self.client.force_login(self.gm)
        resp = self.client.post(reverse("vendor_quotes:revision_void", args=[r0.id]), {
            "comment": "Voided via view test"
        })
        self.assertEqual(resp.status_code, 302)
        r0.refresh_from_db()
        self.assertEqual(r0.status, QuoteRevision.STATUS_VOID)

        # 4. Enquiry delete view
        q3, _ = self._create_quote_with_file(user=self.editor, enq_num="ENQ/2026/11/503")
        enq3 = q3.enquiry
        resp = self.client.post(reverse("vendor_quotes:enquiry_delete", args=[enq3.id]), {
            "confirm_number": enq3.number
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Enquiry.objects.filter(id=enq3.id).exists())

        # 5. Vendor delete and toggle active views
        v_test = Vendor.objects.create(name="Vendor View Test", code="VVT", currency="EUR")
        resp = self.client.post(reverse("vendor_quotes:vendor_toggle_active", args=[v_test.id]))
        self.assertEqual(resp.status_code, 302)
        v_test.refresh_from_db()
        self.assertFalse(v_test.is_active)

        resp = self.client.post(reverse("vendor_quotes:vendor_delete", args=[v_test.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Vendor.objects.filter(id=v_test.id).exists())

        # 6. Master file delete and restore views
        v_mf = Vendor.objects.create(name="Vendor MF View", code="VMF", currency="EUR")
        mf_old = VendorMasterFile(vendor=v_mf, sheet_count=1, note="v1")
        mf_old.file.save("v1.xlsx", SimpleUploadedFile("v1.xlsx", bos_like_workbook().getvalue()))
        mf_new = VendorMasterFile(vendor=v_mf, sheet_count=2, note="v2")
        mf_new.file.save("v2.xlsx", SimpleUploadedFile("v2.xlsx", bos_like_workbook().getvalue()))

        resp = self.client.post(reverse("vendor_quotes:master_file_restore", args=[mf_old.id]))
        self.assertEqual(resp.status_code, 302)
        v_mf.refresh_from_db()
        self.assertIn("Restored from", v_mf.current_master.note)

        resp = self.client.post(reverse("vendor_quotes:master_file_delete", args=[mf_old.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(VendorMasterFile.objects.filter(id=mf_old.id).exists())

        # 7. Master sheet delete and bulk delete views
        self.client.force_login(self.editor)
        ms1 = MasterSheet.objects.create(vendor=v_mf, sheet_name="MS1", enquiry_no="ENQ/1", snapshot={})
        ms2 = MasterSheet.objects.create(vendor=v_mf, sheet_name="MS2", enquiry_no="ENQ/2", snapshot={})
        resp = self.client.post(reverse("vendor_quotes:master_sheet_delete", args=[ms1.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MasterSheet.objects.filter(id=ms1.id).exists())

        resp = self.client.post(reverse("vendor_quotes:master_sheets_bulk_delete", args=[v_mf.id]), {
            "selected_sheets": [ms2.id]
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MasterSheet.objects.filter(id=ms2.id).exists())
