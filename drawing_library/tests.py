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
            project_name="Fuel Gas Conditioning Skid Project",
            customer_name="PT Pertamina EP",
            enquiry_number="ENQ-2026-099",
            po_number="PO-77401",
            pid_reference="PID-SKID-01",
            drawing_name="Skid Piping General Arrangement",
            base_drawing_number="FF-DWG-1001",
            drawing_type="MASTER",
            active_revision_number="0",
            drafter_name="Budi Santoso",
            format="ZWCAD",
            watermark_company="PT FLOW FORCE INDONESIA",
            created_by=self.admin,
        )

        # Create dummy PDF file for upload
        self.dummy_pdf_bytes = create_dummy_pdf()
        self.pdf_file = SimpleUploadedFile(
            "random_local_title_v2.pdf",
            self.dummy_pdf_bytes,
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
            original_pdf_filename="random_local_title_v2.pdf",
            original_native_filename="piping_model.dwg",
            drafter_name="Budi Santoso",
            created_by=self.admin,
        )

    def test_project_root_header_fields(self):
        """Verify Project Root Header fields: PID Reference, Customer Name, Project Name."""
        self.assertEqual(self.drawing.pid_reference, "PID-SKID-01")
        self.assertEqual(self.drawing.customer_name, "PT Pertamina EP")
        self.assertEqual(self.drawing.project_name, "Fuel Gas Conditioning Skid Project")

        client = Client()
        client.force_login(self.admin)
        resp = client.get(reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PID-SKID-01")
        self.assertContains(resp, "PT Pertamina EP")
        self.assertContains(resp, "Fuel Gas Conditioning Skid Project")

    def test_preserve_uploaded_filenames_on_download(self):
        """Downloads must strictly retain the original uploaded filename without auto-renaming."""
        client = Client()
        client.force_login(self.admin)

        # Download PDF
        resp_pdf = client.get(reverse("drawings:download_watermarked_pdf", args=[self.revision.pk]))
        self.assertEqual(resp_pdf.status_code, 200)
        self.assertIn('filename="random_local_title_v2.pdf"', resp_pdf["Content-Disposition"])

        # Inline view PDF
        resp_view = client.get(reverse("drawings:view_watermarked_pdf", args=[self.revision.pk]))
        self.assertEqual(resp_view.status_code, 200)
        self.assertIn('inline; filename="random_local_title_v2.pdf"', resp_view["Content-Disposition"])

        # Download CAD file
        resp_cad = client.get(reverse("drawings:download_native_file", args=[self.revision.pk]))
        self.assertEqual(resp_cad.status_code, 200)
        self.assertIn('filename="piping_model.dwg"', resp_cad["Content-Disposition"])

    def test_clean_pdf_without_watermark(self):
        """PDF download and inline view must return clean PDF data without watermark overlay."""
        client = Client()
        client.force_login(self.admin)

        resp_pdf = client.get(reverse("drawings:download_watermarked_pdf", args=[self.revision.pk]))
        self.assertEqual(resp_pdf.status_code, 200)
        self.assertEqual(resp_pdf.content, self.dummy_pdf_bytes)

        resp_view = client.get(reverse("drawings:view_watermarked_pdf", args=[self.revision.pk]))
        self.assertEqual(resp_view.status_code, 200)
        self.assertEqual(b"".join(resp_view.streaming_content), self.dummy_pdf_bytes)

    def test_3_level_tree_hierarchy_structure(self):
        """Verify Level 1 (Parent) -> Level 2 (Child) -> Level 3 (Grandchild) tree structure."""
        # Level 1 is self.drawing (Master)
        self.assertEqual(self.drawing.level, 1)
        self.assertTrue(self.drawing.is_master)

        # Create Level 2 Child (Sub-Assembly)
        child = EngineeringDrawing.objects.create(
            parent=self.drawing,
            drawing_type="SUB_ASSEMBLY",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Suction Filter Sub-Assembly",
            base_drawing_number="FF-SUB-01",
            drafter_name="Agus Wijaya",
            format="SOLIDWORKS",
            created_by=self.admin,
        )
        child_pdf = SimpleUploadedFile("suction_sub_assy.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        child_cad = SimpleUploadedFile("suction_sub_assy.slddrw", b"CAD-SUB-DATA", content_type="application/octet-stream")
        DrawingRevision.objects.create(
            drawing=child,
            revision_number="0",
            stage_change_description="Sub-assembly released for review",
            pdf_file=child_pdf,
            native_file=child_cad,
            original_pdf_filename="suction_sub_assy.pdf",
            original_native_filename="suction_sub_assy.slddrw",
            drafter_name="Agus Wijaya",
            created_by=self.admin,
        )

        self.assertEqual(child.level, 2)
        self.assertTrue(child.is_child)
        self.assertEqual(child.root_drawing, self.drawing)

        # Create Level 3 Grandchild (Detail Fabrication Part)
        grandchild = EngineeringDrawing.objects.create(
            parent=child,
            drawing_type="DETAIL_PART",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Suction Flange Detail Plate",
            base_drawing_number="FF-DET-001",
            drafter_name="Dewi Sartika",
            format="AUTOCAD",
            created_by=self.admin,
        )
        grandchild_pdf = SimpleUploadedFile("flange_detail_p1.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        DrawingRevision.objects.create(
            drawing=grandchild,
            revision_number="A",
            stage_change_description="Detail cutting sizes updated",
            pdf_file=grandchild_pdf,
            original_pdf_filename="flange_detail_p1.pdf",
            drafter_name="Dewi Sartika",
            created_by=self.admin,
        )

        self.assertEqual(grandchild.level, 3)
        self.assertTrue(grandchild.is_grandchild)
        self.assertEqual(grandchild.root_drawing, self.drawing)

        # Verify page renders all 3 levels with interactive tree elements
        client = Client()
        client.force_login(self.admin)
        resp = client.get(reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertEqual(resp.status_code, 200)

        # Level 1 elements
        self.assertContains(resp, "FF-DWG-1001")
        self.assertContains(resp, "Level 1 (Parent Branch)")
        self.assertContains(resp, "Add Child Drawing")
        self.assertContains(resp, "btn-tree-toggle")

        # Level 2 elements
        self.assertContains(resp, "FF-SUB-01")
        self.assertContains(resp, "Level 2 (Child Branch - Sub-Assembly)")
        self.assertContains(resp, "Add Grandchild Drawing")

        # Level 3 elements
        self.assertContains(resp, "FF-DET-001")
        self.assertContains(resp, "Level 3 (Grandchild - Detail Part)")

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
        self.assertContains(resp, "PID-SKID-01")

        # Engineer can view drawing detail
        resp_detail = client.get(reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertEqual(resp_detail.status_code, 200)
        self.assertContains(resp_detail, "PT Pertamina EP")

        # Engineer can download clean PDF retaining original uploaded filename
        resp_pdf = client.get(reverse("drawings:download_watermarked_pdf", args=[self.revision.pk]))
        self.assertEqual(resp_pdf.status_code, 200)
        self.assertEqual(resp_pdf["Content-Type"], "application/pdf")
        self.assertIn("random_local_title_v2.pdf", resp_pdf["Content-Disposition"])

    def test_access_control_inherited_to_child_and_grandchild(self):
        """Clearance granted on Master drawing cascades to sub-assemblies and detail parts."""
        # Create Child
        child = EngineeringDrawing.objects.create(
            parent=self.drawing,
            drawing_type="SUB_ASSEMBLY",
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Sub-Assembly Skid Unit",
            base_drawing_number="FF-SUB-88",
            drafter_name="Agus Wijaya",
            created_by=self.admin,
        )
        # Create Grandchild
        grandchild = EngineeringDrawing.objects.create(
            parent=child,
            drawing_type="DETAIL_PART",
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Detail Flange Gasket",
            base_drawing_number="FF-DET-88-01",
            drafter_name="Dewi",
            created_by=self.admin,
        )

        # Grant access ONLY to the Master (self.drawing)
        DrawingAccess.objects.create(
            drawing=self.drawing,
            user=self.engineer,
            access_level="VIEW",
            granted_by=self.admin,
        )

        client = Client()
        client.force_login(self.engineer)

        # Engineer can access child and grandchild detail pages
        resp_child = client.get(reverse("drawings:drawing_detail", args=[child.pk]))
        self.assertEqual(resp_child.status_code, 200)

        resp_grandchild = client.get(reverse("drawings:drawing_detail", args=[grandchild.pk]))
        self.assertEqual(resp_grandchild.status_code, 200)

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

    def test_drawing_create_post_success(self):
        """Verify submitting the streamlined form creates drawing and initial revision."""
        client = Client()
        client.force_login(self.admin)

        post_data = {
            "customer_name": "Chevron Pacific Indonesia",
            "project_name": "Steam Injection Manifold",
            "pid_reference": "PID-STM-900",
            "drafter_name": "Hendro Pratama",
            "base_drawing_number": "FF-NEW-9001",
            "drawing_name": "Steam Header Assembly",
            "description": "High pressure steam header assembly",
            "format": "ZWCAD",
            "initial_revision_number": "0",
            "initial_stage_description": "Initial design",
            "initial_pdf_file": SimpleUploadedFile("steam_header_v1.pdf", self.dummy_pdf_bytes, content_type="application/pdf"),
            "initial_native_file": SimpleUploadedFile("steam_header_v1.dwg", b"NATIVE-DWG", content_type="application/acad"),
        }

        resp = client.post(reverse("drawings:drawing_create"), post_data)
        self.assertEqual(resp.status_code, 302)

        new_drawing = EngineeringDrawing.objects.get(base_drawing_number="FF-NEW-9001")
        self.assertEqual(new_drawing.customer_name, "Chevron Pacific Indonesia")
        self.assertEqual(new_drawing.project_name, "Steam Injection Manifold")
        self.assertEqual(new_drawing.level, 1)
        self.assertEqual(new_drawing.revisions.count(), 1)
        rev = new_drawing.latest_revision
        self.assertEqual(rev.original_pdf_filename, "steam_header_v1.pdf")
        self.assertEqual(rev.original_native_filename, "steam_header_v1.dwg")

    def test_first_page_only_pid_starter_details(self):
        """First page must display PID starter details (PID reference, customer, project name, drafter) with counts."""
        client = Client()
        client.force_login(self.admin)

        resp = client.get(reverse("drawings:drawing_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PID Project Starter")
        self.assertContains(resp, "PID-SKID-01")
        self.assertContains(resp, "PT Pertamina EP")
        self.assertContains(resp, "Fuel Gas Conditioning Skid Project")
        self.assertContains(resp, "Budi Santoso")
        self.assertContains(resp, "Open Project Hierarchy")

    def test_revisions_on_parent_child_and_grandchild_with_previous_versions_intact(self):
        """
        Revisions can be loaded to all/each childs, parents, grandchilds.
        After revising, previous versions must remain present and accessible.
        """
        client = Client()
        client.force_login(self.admin)

        # 1. Create Level 2 Child
        child = EngineeringDrawing.objects.create(
            parent=self.drawing,
            drawing_type="SUB_ASSEMBLY",
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            project_name=self.drawing.project_name,
            drawing_name="Sub-Assembly Skid Manifold",
            base_drawing_number="FF-SUB-001",
            drafter_name="Agus Drafter",
            created_by=self.admin,
        )
        rev0_child_pdf = SimpleUploadedFile("child_rev0.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        rev0_child = DrawingRevision.objects.create(
            drawing=child,
            revision_number="0",
            stage_change_description="Initial child release",
            pdf_file=rev0_child_pdf,
            original_pdf_filename="child_rev0.pdf",
            drafter_name="Agus Drafter",
            created_by=self.admin,
        )
        child.sync_active_revision()

        # 2. Create Level 3 Grandchild
        grandchild = EngineeringDrawing.objects.create(
            parent=child,
            drawing_type="DETAIL_PART",
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            project_name=self.drawing.project_name,
            drawing_name="Detail Flange Spec",
            base_drawing_number="FF-DET-001",
            drafter_name="Siti Drafter",
            created_by=self.admin,
        )
        rev0_gc_pdf = SimpleUploadedFile("gc_rev0.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        rev0_gc = DrawingRevision.objects.create(
            drawing=grandchild,
            revision_number="0",
            stage_change_description="Initial detail part release",
            pdf_file=rev0_gc_pdf,
            original_pdf_filename="gc_rev0.pdf",
            drafter_name="Siti Drafter",
            created_by=self.admin,
        )
        grandchild.sync_active_revision()

        # Verify initial states
        self.assertEqual(self.drawing.level, 1)
        self.assertEqual(child.level, 2)
        self.assertEqual(grandchild.level, 3)
        self.assertEqual(child.active_revision_number, "0")
        self.assertEqual(grandchild.active_revision_number, "0")

        # 3. Post a revision to Parent (self.drawing)
        rev1_parent_pdf = SimpleUploadedFile("parent_rev1.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        resp_p = client.post(
            reverse("drawings:revision_create", args=[self.drawing.pk]),
            {
                "revision_number": "1",
                "drafter_name": "Budi Santoso",
                "stage_change_description": "Piping routing updated per client notes",
                "pdf_file": rev1_parent_pdf,
            }
        )
        self.assertEqual(resp_p.status_code, 302)
        self.drawing.refresh_from_db()
        self.assertEqual(self.drawing.active_revision_number, "1")
        self.assertEqual(self.drawing.revisions.count(), 2)
        self.assertEqual(len(self.drawing.previous_revisions), 1)

        # Verify Rev 0 and Rev 1 PDFs both exist on disk
        rev0_parent = self.drawing.previous_revisions[0]
        rev1_parent = self.drawing.latest_revision
        self.assertTrue(os.path.exists(rev0_parent.pdf_file.path))
        self.assertTrue(os.path.exists(rev1_parent.pdf_file.path))
        self.assertNotEqual(rev0_parent.pdf_file.path, rev1_parent.pdf_file.path)

        # 4. Post a revision to Level 2 Child
        rev1_child_pdf = SimpleUploadedFile("child_rev1.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        resp_c = client.post(
            reverse("drawings:revision_create", args=[child.pk]),
            {
                "revision_number": "1",
                "drafter_name": "Agus Drafter",
                "stage_change_description": "Sub-assembly welds revised",
                "pdf_file": rev1_child_pdf,
            }
        )
        self.assertEqual(resp_c.status_code, 302)
        # Verify redirect goes to root_project
        self.assertIn(reverse("drawings:drawing_detail", args=[self.drawing.pk]), resp_c.url)
        child.refresh_from_db()
        self.assertEqual(child.active_revision_number, "1")
        self.assertEqual(child.revisions.count(), 2)
        self.assertEqual(len(child.previous_revisions), 1)
        self.assertTrue(os.path.exists(child.previous_revisions[0].pdf_file.path))
        self.assertTrue(os.path.exists(child.latest_revision.pdf_file.path))

        # 5. Post a revision to Level 3 Grandchild
        rev1_gc_pdf = SimpleUploadedFile("gc_rev1.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        resp_gc = client.post(
            reverse("drawings:revision_create", args=[grandchild.pk]),
            {
                "revision_number": "A",
                "drafter_name": "Siti Drafter",
                "stage_change_description": "Bolt hole tolerances revised",
                "pdf_file": rev1_gc_pdf,
            }
        )
        self.assertEqual(resp_gc.status_code, 302)
        self.assertIn(reverse("drawings:drawing_detail", args=[self.drawing.pk]), resp_gc.url)
        grandchild.refresh_from_db()
        self.assertEqual(grandchild.active_revision_number, "A")
        self.assertEqual(grandchild.revisions.count(), 2)
        self.assertEqual(len(grandchild.previous_revisions), 1)
        self.assertTrue(os.path.exists(grandchild.previous_revisions[0].pdf_file.path))
        self.assertTrue(os.path.exists(grandchild.latest_revision.pdf_file.path))

        # 6. Check Project Tree view renders all levels and previous revision sections
        resp_tree = client.get(reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertEqual(resp_tree.status_code, 200)
        self.assertContains(resp_tree, "FF-DWG-1001")
        self.assertContains(resp_tree, "FF-SUB-001")
        self.assertContains(resp_tree, "FF-DET-001")
        self.assertContains(resp_tree, "Past Revisions (1)")

    def test_full_access_edit_clearance_allows_create_edit_upload_download(self):
        """
        When an employee is given EDIT (Full Access) clearance:
        They have full access to create drawings, edit metadata, upload revisions, and download files.
        """
        # Grant global EDIT ("Full Access") to engineer
        DrawingAccess.objects.create(
            drawing=None,
            user=self.engineer,
            access_level="EDIT",
            granted_by=self.admin,
        )

        client = Client()
        client.force_login(self.engineer)

        # 1. Check list view has + New PID Project button visible
        resp_list = client.get(reverse("drawings:drawing_list"))
        self.assertEqual(resp_list.status_code, 200)
        self.assertContains(resp_list, "+ New PID Project")
        self.assertContains(resp_list, "Add Parent")

        # 2. Engineer can CREATE a new drawing project
        post_data = {
            "customer_name": "TotalEnergies E&P",
            "project_name": "Condensate Booster System",
            "pid_reference": "PID-BOOST-500",
            "drafter_name": "Design Engineer",
            "base_drawing_number": "FF-ENG-0500",
            "drawing_name": "Booster Skid Layout",
            "format": "ZWCAD",
            "initial_revision_number": "0",
            "initial_stage_description": "First draft release",
        }
        resp_create = client.post(reverse("drawings:drawing_create"), post_data)
        self.assertEqual(resp_create.status_code, 302)
        created_dwg = EngineeringDrawing.objects.get(base_drawing_number="FF-ENG-0500")
        self.assertEqual(created_dwg.customer_name, "TotalEnergies E&P")

        # 3. Engineer can EDIT metadata
        edit_data = {
            "customer_name": "TotalEnergies E&P",
            "project_name": "Condensate Booster System",
            "pid_reference": "PID-BOOST-500",
            "drafter_name": "Design Engineer",
            "base_drawing_number": "FF-ENG-0500",
            "drawing_name": "Booster Skid Layout - Finalized Name",
            "format": "ZWCAD",
        }
        resp_edit = client.post(reverse("drawings:drawing_edit", args=[created_dwg.pk]), edit_data)
        self.assertEqual(resp_edit.status_code, 302)
        created_dwg.refresh_from_db()
        self.assertEqual(created_dwg.drawing_name, "Booster Skid Layout - Finalized Name")

        # 4. Engineer can UPLOAD revisions
        rev_pdf = SimpleUploadedFile("booster_rev1.pdf", self.dummy_pdf_bytes, content_type="application/pdf")
        resp_rev = client.post(
            reverse("drawings:revision_create", args=[created_dwg.pk]),
            {
                "revision_number": "1",
                "drafter_name": "Design Engineer",
                "stage_change_description": "Approved for HAZOP",
                "pdf_file": rev_pdf,
            }
        )
        self.assertEqual(resp_rev.status_code, 302)
        created_dwg.refresh_from_db()
        self.assertEqual(created_dwg.active_revision_number, "1")

        # 5. Engineer can DOWNLOAD PDF and CAD
        latest_rev = created_dwg.latest_revision
        resp_dl = client.get(reverse("drawings:download_watermarked_pdf", args=[latest_rev.pk]))
        self.assertEqual(resp_dl.status_code, 200)
        self.assertEqual(resp_dl["Content-Type"], "application/pdf")

        # 6. Engineer can CREATE a child drawing under created_dwg
        child_post = {
            "customer_name": created_dwg.customer_name,
            "project_name": created_dwg.project_name,
            "pid_reference": created_dwg.pid_reference,
            "drafter_name": "Design Engineer",
            "base_drawing_number": "FF-ENG-0500-CH1",
            "drawing_name": "Booster Piping Loop",
            "format": "ZWCAD",
            "parent": created_dwg.pk,
            "drawing_type": "SUB_ASSEMBLY",
            "initial_revision_number": "0",
            "initial_stage_description": "Sub-assembly draft",
        }
        resp_child = client.post(reverse("drawings:drawing_create"), child_post)
        self.assertEqual(resp_child.status_code, 302)
        child_dwg = EngineeringDrawing.objects.get(base_drawing_number="FF-ENG-0500-CH1")
        self.assertEqual(child_dwg.parent, created_dwg)
        self.assertEqual(child_dwg.level, 2)

    def test_delete_grandchild_drawing(self):
        """Test deleting a Level 3 (Grandchild) drawing leaves Parent and Child intact."""
        child = EngineeringDrawing.objects.create(
            parent=self.drawing,
            drawing_type="SUB_ASSEMBLY",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Sub Assembly 1",
            base_drawing_number="FF-DEL-CH1",
            active_revision_number="0",
            drafter_name="Drafter",
            format="ZWCAD",
            created_by=self.admin,
        )
        grandchild = EngineeringDrawing.objects.create(
            parent=child,
            drawing_type="DETAIL_PART",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Detail Part 1",
            base_drawing_number="FF-DEL-GC1",
            active_revision_number="0",
            drafter_name="Drafter",
            format="ZWCAD",
            created_by=self.admin,
        )

        client = Client()
        client.force_login(self.admin)

        resp = client.post(reverse("drawings:drawing_delete", args=[grandchild.pk]))
        # Should redirect back to root drawing detail
        self.assertRedirects(resp, reverse("drawings:drawing_detail", args=[self.drawing.pk]))

        # Grandchild deleted, child and parent intact
        self.assertFalse(EngineeringDrawing.objects.filter(pk=grandchild.pk).exists())
        self.assertTrue(EngineeringDrawing.objects.filter(pk=child.pk).exists())
        self.assertTrue(EngineeringDrawing.objects.filter(pk=self.drawing.pk).exists())

    def test_delete_child_drawing_cascades_grandchildren(self):
        """Test deleting a Level 2 (Child) drawing deletes its grandchildren while keeping parent intact."""
        child = EngineeringDrawing.objects.create(
            parent=self.drawing,
            drawing_type="SUB_ASSEMBLY",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Sub Assembly 2",
            base_drawing_number="FF-DEL-CH2",
            active_revision_number="0",
            drafter_name="Drafter",
            format="ZWCAD",
            created_by=self.admin,
        )
        grandchild = EngineeringDrawing.objects.create(
            parent=child,
            drawing_type="DETAIL_PART",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Detail Part 2",
            base_drawing_number="FF-DEL-GC2",
            active_revision_number="0",
            drafter_name="Drafter",
            format="ZWCAD",
            created_by=self.admin,
        )

        client = Client()
        client.force_login(self.admin)

        resp = client.post(reverse("drawings:drawing_delete", args=[child.pk]))
        self.assertRedirects(resp, reverse("drawings:drawing_detail", args=[self.drawing.pk]))

        self.assertFalse(EngineeringDrawing.objects.filter(pk=child.pk).exists())
        self.assertFalse(EngineeringDrawing.objects.filter(pk=grandchild.pk).exists())
        self.assertTrue(EngineeringDrawing.objects.filter(pk=self.drawing.pk).exists())

    def test_delete_parent_drawing_cascades_entire_branch(self):
        """Test deleting a Level 1 (Parent) drawing deletes the entire branch and redirects to list or other parent."""
        child = EngineeringDrawing.objects.create(
            parent=self.drawing,
            drawing_type="SUB_ASSEMBLY",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Sub Assembly 3",
            base_drawing_number="FF-DEL-CH3",
            active_revision_number="0",
            drafter_name="Drafter",
            format="ZWCAD",
            created_by=self.admin,
        )
        grandchild = EngineeringDrawing.objects.create(
            parent=child,
            drawing_type="DETAIL_PART",
            project_name=self.drawing.project_name,
            customer_name=self.drawing.customer_name,
            pid_reference=self.drawing.pid_reference,
            drawing_name="Detail Part 3",
            base_drawing_number="FF-DEL-GC3",
            active_revision_number="0",
            drafter_name="Drafter",
            format="ZWCAD",
            created_by=self.admin,
        )

        client = Client()
        client.force_login(self.admin)

        resp = client.post(reverse("drawings:drawing_delete", args=[self.drawing.pk]))
        self.assertRedirects(resp, reverse("drawings:drawing_list"))

        self.assertFalse(EngineeringDrawing.objects.filter(pk=self.drawing.pk).exists())
        self.assertFalse(EngineeringDrawing.objects.filter(pk=child.pk).exists())
        self.assertFalse(EngineeringDrawing.objects.filter(pk=grandchild.pk).exists())

    def test_delete_drawing_at_different_stages_and_revisions(self):
        """Test deleting a drawing at different stages (with multiple revisions and stage notes)."""
        rev1 = DrawingRevision.objects.create(
            drawing=self.drawing,
            revision_number="1",
            drafter_name="Senior Drafter",
            stage_change_description="HAZOP Stage Review Completed",
            created_by=self.admin,
        )
        self.drawing.sync_active_revision()
        self.assertEqual(self.drawing.active_revision_number, "1")

        client = Client()
        client.force_login(self.admin)

        # First test deleting individual revision stage
        resp_rev = client.post(reverse("drawings:revision_delete", args=[rev1.pk]))
        self.assertRedirects(resp_rev, reverse("drawings:drawing_detail", args=[self.drawing.pk]))
        self.assertFalse(DrawingRevision.objects.filter(pk=rev1.pk).exists())

        # Then test deleting drawing itself at initial stage
        resp_dwg = client.post(reverse("drawings:drawing_delete", args=[self.drawing.pk]))
        self.assertRedirects(resp_dwg, reverse("drawings:drawing_list"))
        self.assertFalse(EngineeringDrawing.objects.filter(pk=self.drawing.pk).exists())

    def test_unauthorized_user_cannot_delete_drawing(self):
        """Users without admin or edit clearance cannot delete drawings."""
        client = Client()
        client.force_login(self.unauthorized_user)

        resp = client.post(reverse("drawings:drawing_delete", args=[self.drawing.pk]))
        # Should be denied and drawing still exists
        self.assertEqual(EngineeringDrawing.objects.filter(pk=self.drawing.pk).count(), 1)




