import io
import os
from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from auth_app.models import EmployeeUser
from employee_management.models import Department
from drawing_library.models import EngineeringDrawing, DrawingRevision, DrawingAccess
from drawing_library.watermark import apply_company_watermark, create_corner_watermark_page


def create_dummy_pdf():
    """Helper to generate a minimal valid 1-page PDF in memory."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.drawString(100, 750, "Engineering Drawing Test Specimen")
    c.save()
    buf.seek(0)
    return buf.getvalue()


class DrawingLibraryTests(TestCase):
    def setUp(self):
        # Create users
        self.super_admin = EmployeeUser.objects.create_user(
            email="superadmin@flowforce.com",
            full_name="Super Admin User",
            password="Password123!",
            role="SUPER_ADMIN",
            is_staff=True,
            status="APPROVED",
        )
        self.admin = EmployeeUser.objects.create_user(
            email="admin@flowforce.com",
            full_name="Admin User",
            password="Password123!",
            role="ADMIN",
            is_staff=True,
            status="APPROVED",
        )
        self.department = Department.objects.create(name="Engineering & Design")
        self.engineer = EmployeeUser.objects.create_user(
            email="engineer@flowforce.com",
            full_name="Design Engineer",
            password="Password123!",
            role="EMPLOYEE",
            department=self.department,
            status="APPROVED",
        )
        self.unauthorized_user = EmployeeUser.objects.create_user(
            email="other@flowforce.com",
            full_name="Other Employee",
            password="Password123!",
            role="EMPLOYEE",
            status="APPROVED",
        )

        # Create test drawing project
        self.drawing = EngineeringDrawing.objects.create(
            customer_name="PT Pertamina EP",
            enquiry_number="ENQ-2026-099",
            po_number="PO-77401",
            pid_reference="PID-SKID-01",
            drawing_name="Skid Piping General Arrangement",
            base_drawing_number="FF-DWG-1001",
            active_revision_number="0",
            drafter_name="Budi Santoso",
            format="ZWCAD",
            watermark_company="PT FLOW FORCE INDONESIA",
            created_by=self.admin,
        )

        # Create dummy PDF file for upload
        dummy_pdf_bytes = create_dummy_pdf()
        self.pdf_file = SimpleUploadedFile(
            "random_local_title_v2.pdf",
            dummy_pdf_bytes,
            content_type="application/pdf"
        )
        # Dummy native CAD file (.dwg)
        self.dwg_file = SimpleUploadedFile(
            "piping_model.dwg",
            b"CAD-NATIVE-BINARY-DATA-SAMPLE",
            content_type="application/acad"
        )

        self.revision = DrawingRevision.objects.create(
            drawing=self.drawing,
            revision_number="0",
            stage_change_description="Initial IFC Release",
            native_file=self.dwg_file,
            pdf_file=self.pdf_file,
            drafter_name="Budi Santoso",
            created_by=self.admin,
        )

    def test_project_metadata_header_fields(self):
        """Verify all 10 required fields in project metadata header."""
        self.assertEqual(self.drawing.customer_name, "PT Pertamina EP")
        self.assertEqual(self.drawing.enquiry_number, "ENQ-2026-099")
        self.assertEqual(self.drawing.po_number, "PO-77401")
        self.assertEqual(self.drawing.pid_reference, "PID-SKID-01")
        self.assertEqual(self.drawing.drawing_name, "Skid Piping General Arrangement")
        self.assertEqual(self.drawing.base_drawing_number, "FF-DWG-1001")
        self.assertEqual(self.drawing.active_revision_number, "0")
        self.assertEqual(self.drawing.drafter_name, "Budi Santoso")
        self.assertEqual(self.drawing.format, "ZWCAD")
        self.assertEqual(self.drawing.watermark_company, "PT FLOW FORCE INDONESIA")

    def test_auto_naming_of_pdf_upload(self):
        """Uploaded PDF must be automatically renamed to [CustomerName]_[PID]_[DrawingName]_[Rev#].pdf."""
        expected_filename = "PT_Pertamina_EP_PID-SKID-01_Skid_Piping_General_Arrangement_0.pdf"
        actual_filename = os.path.basename(self.revision.pdf_file.name)
        self.assertEqual(actual_filename, expected_filename)
        self.assertEqual(self.revision.expected_pdf_filename(), expected_filename)

    def test_multiple_drawings_under_one_pid_with_different_names(self):
        """Verify multiple drawings can be saved under one PID with different names and distinct PDF filenames."""
        # Create second drawing under the same PID reference "PID-SKID-01" with a different name
        drawing2 = EngineeringDrawing.objects.create(
            customer_name="PT Pertamina EP",
            pid_reference="PID-SKID-01",
            drawing_name="Structural Skid Base Frame",
            base_drawing_number="FF-DWG-1002",
            drafter_name="Agus Wijaya",
            format="SOLIDWORKS",
            watermark_company="PT FLOW FORCE INDONESIA",
            created_by=self.admin,
        )

        pdf2 = SimpleUploadedFile("local_export_frame.pdf", create_dummy_pdf(), content_type="application/pdf")
        rev2 = DrawingRevision.objects.create(
            drawing=drawing2,
            revision_number="0",
            stage_change_description="Structural steel calculations approved",
            pdf_file=pdf2,
            drafter_name="Agus Wijaya",
            created_by=self.admin,
        )

        # Both drawings share the same PID
        self.assertEqual(self.drawing.pid_reference, drawing2.pid_reference)
        # But have different drawing names
        self.assertNotEqual(self.drawing.drawing_name, drawing2.drawing_name)

        # Verify their filenames do not collide and clearly distinguish the drawings
        filename1 = os.path.basename(self.revision.pdf_file.name)
        filename2 = os.path.basename(rev2.pdf_file.name)

        self.assertEqual(filename1, "PT_Pertamina_EP_PID-SKID-01_Skid_Piping_General_Arrangement_0.pdf")
        self.assertEqual(filename2, "PT_Pertamina_EP_PID-SKID-01_Structural_Skid_Base_Frame_0.pdf")
        self.assertNotEqual(filename1, filename2)

        # Check in list view grouping
        client = Client()
        client.force_login(self.admin)
        resp = client.get(reverse("drawings:drawing_list") + "?view=pid")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PID-SKID-01")
        self.assertContains(resp, "Skid Piping General Arrangement")
        self.assertContains(resp, "Structural Skid Base Frame")

    def test_native_file_accepted_without_warning(self):
        """Native files (.dwg, .dwt, .slddrw) must be accepted directly."""
        self.assertTrue(bool(self.revision.native_file))
        self.assertTrue("piping_model" in self.revision.native_file.name)

    def test_revision_log_and_active_revision_sync(self):
        """Adding a new revision should update the active revision on the drawing."""
        new_pdf = SimpleUploadedFile("local_export_rev_A.pdf", create_dummy_pdf(), content_type="application/pdf")
        rev_a = DrawingRevision.objects.create(
            drawing=self.drawing,
            revision_number="A",
            stage_change_description="Revised tie-in locations as per client feedback",
            pdf_file=new_pdf,
            drafter_name="Budi Santoso",
            created_by=self.admin,
        )
        self.drawing.refresh_from_db()
        self.assertEqual(self.drawing.active_revision_number, "A")
        self.assertEqual(self.drawing.revisions.count(), 2)

    def test_access_control_unauthorized_user_blocked(self):
        """Users without access cannot view the drawing library or specific drawings."""
        client = Client()
        client.force_login(self.unauthorized_user)

        # Attempt to access list view
        resp = client.get(reverse("drawings:drawing_list"))
        # Should redirect with error
        self.assertNotEqual(resp.status_code, 200)

        # Attempt to access detail view
        resp_detail = client.get(reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertNotEqual(resp_detail.status_code, 200)

        # Attempt to download PDF
        resp_pdf = client.get(reverse("drawings:download_watermarked_pdf", args=[self.revision.pk]))
        self.assertNotEqual(resp_pdf.status_code, 200)

    def test_access_control_assigned_by_admin(self):
        """When Admin grants access, the user can view and download watermarked PDF."""
        # Grant VIEW access to engineer
        DrawingAccess.objects.create(
            drawing=self.drawing,
            user=self.engineer,
            access_level="VIEW",
            granted_by=self.admin,
        )

        client = Client()
        client.force_login(self.engineer)

        # Engineer can now access drawing list
        resp = client.get(reverse("drawings:drawing_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "FF-DWG-1001")

        # Engineer can view drawing detail
        resp_detail = client.get(reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertEqual(resp_detail.status_code, 200)
        self.assertContains(resp, "PT Pertamina EP")

        # Engineer can download watermarked PDF
        resp_pdf = client.get(reverse("drawings:download_watermarked_pdf", args=[self.revision.pk]))
        self.assertEqual(resp_pdf.status_code, 200)
        self.assertEqual(resp_pdf["Content-Type"], "application/pdf")
        self.assertIn("PT_Pertamina_EP_PID-SKID-01_Skid_Piping_General_Arrangement_0.pdf", resp_pdf["Content-Disposition"])

    def test_admin_and_superadmin_full_access(self):
        """Admins and Super Admins have full access and can grant access."""
        client = Client()
        client.force_login(self.super_admin)

        resp = client.get(reverse("drawings:drawing_list"))
        self.assertEqual(resp.status_code, 200)

        resp_access = client.get(reverse("drawings:global_access_management"))
        self.assertEqual(resp_access.status_code, 200)

    def test_semi_transparent_watermark_generation(self):
        """Watermark engine correctly merges watermark onto PDF pages."""
        raw_pdf = create_dummy_pdf()
        watermarked_io = apply_company_watermark(raw_pdf, "PT FLOW FORCE INDONESIA")
        self.assertIsNotNone(watermarked_io)
        
        # Verify valid readable PDF produced
        reader = PdfReader(watermarked_io)
        self.assertEqual(len(reader.pages), 1)

        # Test with alternative company watermark
        watermarked_io_eng = apply_company_watermark(raw_pdf, "PT FLOW FORCE ENGINEERING")
        reader_eng = PdfReader(watermarked_io_eng)
        self.assertEqual(len(reader_eng.pages), 1)

    def test_drawing_activity_logging(self):
        """Verify employee activity logs are created on view, download, and access changes."""
        from drawing_library.models import DrawingActivityLog

        # Grant access to engineer
        DrawingAccess.objects.create(
            drawing=self.drawing,
            user=self.engineer,
            access_level="VIEW",
            granted_by=self.admin,
        )

        client = Client()
        client.force_login(self.engineer)

        # 1. View detail page -> generates VIEW_DRAWING activity
        client.get(reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertTrue(
            DrawingActivityLog.objects.filter(user=self.engineer, action="VIEW_DRAWING", drawing=self.drawing).exists()
        )

        # 2. Download watermarked PDF -> generates DOWNLOAD_PDF activity
        client.get(reverse("drawings:download_watermarked_pdf", args=[self.revision.pk]))
        self.assertTrue(
            DrawingActivityLog.objects.filter(user=self.engineer, action="DOWNLOAD_PDF", drawing=self.drawing).exists()
        )

        # 3. Download native CAD file -> generates DOWNLOAD_NATIVE activity
        client.get(reverse("drawings:download_native_file", args=[self.revision.pk]))
        self.assertTrue(
            DrawingActivityLog.objects.filter(user=self.engineer, action="DOWNLOAD_NATIVE", drawing=self.drawing).exists()
        )

