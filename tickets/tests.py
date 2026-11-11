"""
Comprehensive test suite for Tickets & Service Jobs module with Synology integration.
"""
import io
import os
import tempfile
import zipfile
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Ticket, TicketEvent, TicketFile, TicketShareLink
from .services import file_handler, synology, workflow, zoho_lookup
from mcp_server.tools import tickets as mcp_tickets

User = get_user_model()


class TicketNumberAndModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="test@flowforce.com", full_name="Test User", password="password123")

    def test_auto_number_generation(self):
        """Ticket numbers follow TKT-YY-NNNN format and increment per year."""
        current_year = timezone.now().year % 100
        t1 = Ticket.objects.create(title="Valve Repair 1", created_by=self.user)
        self.assertEqual(t1.number, f"TKT-{current_year:02d}-0001")

        t2 = Ticket.objects.create(title="Valve Repair 2", created_by=self.user)
        self.assertEqual(t2.number, f"TKT-{current_year:02d}-0002")

    def test_warranty_status_properties(self):
        """Tests active, expiring soon, and remaining days properties."""
        today = timezone.localdate()
        t = Ticket.objects.create(
            title="Warranty Check",
            warranty_start=today - timezone.timedelta(days=10),
            warranty_end=today + timezone.timedelta(days=15),
            created_by=self.user
        )
        self.assertTrue(t.is_warranty_active)
        self.assertTrue(t.is_warranty_expiring_soon)
        self.assertEqual(t.warranty_days_remaining, 15)

        # Expired warranty
        t.warranty_end = today - timezone.timedelta(days=1)
        t.save()
        self.assertFalse(t.is_warranty_active)
        self.assertFalse(t.is_warranty_expiring_soon)
        self.assertEqual(t.warranty_days_remaining, -1)


class SynologyPathAndSafetyTests(TestCase):
    def test_path_sanitization(self):
        """Path segments strip invalid characters, traversal dots, and limit lengths."""
        dirty = "../../Illegal:/*?<>|Name.. "
        clean = synology.sanitize_path_segment(dirty)
        self.assertNotIn("..", clean)
        self.assertNotIn("/", clean)
        self.assertNotIn("\\", clean)
        self.assertNotIn(":", clean)
        self.assertNotIn("?", clean)
        self.assertTrue(len(clean) <= 80)

    @override_settings(SYNOLOGY_ROOT="/FlowForce/Jobs")
    def test_ticket_nas_folder_builder(self):
        """NAS folder paths are properly rooted and formatted."""
        t1 = Ticket(number="TKT-26-0001", job_no="1921", customer_name="Indofood Sukses", title="Rotary Valve")
        folder = synology.build_ticket_nas_folder(t1)
        self.assertTrue(folder.startswith("/FlowForce/Jobs/"))
        self.assertIn("JOB-1921", folder)
        self.assertIn("Indofood", folder)

        # Enquiry fallback
        t2 = Ticket(number="TKT-26-0002", enquiry_number="ENQ/2026/09/2708", customer_name="Indofood", title="Airlock")
        folder2 = synology.build_ticket_nas_folder(t2)
        self.assertTrue(folder2.startswith("/FlowForce/Jobs/"))
        self.assertIn("ENQ-ENQ-2026-09-2708", folder2)


class ZohoRegexMatchingTests(TestCase):
    def test_job_no_regex_precision(self):
        """Exact regex matching prevents partial matching on numbers (e.g. 1921 vs 19210)."""
        text_match = "SO - 260094 DP / JOB No. 1921"
        text_sub = "SO - 260094 DP / JOB No. 19210"

        matches_exact = zoho_lookup.JOB_REGEX.findall(text_match)
        self.assertIn("1921", matches_exact)

        matches_sub = zoho_lookup.JOB_REGEX.findall(text_sub)
        self.assertIn("19210", matches_sub)
        # Verify filtering logic
        clean_target = "1921"
        self.assertTrue(any(m == clean_target for m in matches_exact))
        self.assertFalse(any(m == clean_target for m in matches_sub))


class StatusTransitionAndRulesTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="admin@flowforce.com", full_name="Admin User", role="SUPER_ADMIN", is_superuser=True)
        self.engineer = User.objects.create_user(email="tech@flowforce.com", full_name="Tech User", role="EMPLOYEE")
        self.other_user = User.objects.create_user(email="other@flowforce.com", full_name="Other User", role="EMPLOYEE")

    def test_project_handover_requires_site_report_and_photo(self):
        """Project moving to HANDOVER requires at least 1 Site Report and 1 Photo."""
        ticket = Ticket.objects.create(
            type=Ticket.TYPE_PROJECT,
            title="Project Alpha",
            status=Ticket.STATUS_INSTALLATION,
            assigned_to=self.engineer,
            created_by=self.engineer
        )

        # Fails without files
        valid, is_override, err = workflow.validate_status_transition(ticket, Ticket.STATUS_HANDOVER, self.engineer)
        self.assertFalse(valid)
        self.assertIn("Site Report", err)
        self.assertIn("Photo", err)

        # Add Photo only
        TicketFile.objects.create(ticket=ticket, category=TicketFile.CATEGORY_PHOTO, original_name="p1.jpg")
        valid, _, err = workflow.validate_status_transition(ticket, Ticket.STATUS_HANDOVER, self.engineer)
        self.assertFalse(valid)
        self.assertIn("Site Report", err)

        # Add Site Report
        TicketFile.objects.create(ticket=ticket, category=TicketFile.CATEGORY_SITE_REPORT, original_name="report.pdf")
        valid, _, _ = workflow.validate_status_transition(ticket, Ticket.STATUS_HANDOVER, self.engineer)
        self.assertTrue(valid)

    def test_project_closed_requires_warranty(self):
        """Closing a project requires at least 1 Warranty document."""
        ticket = Ticket.objects.create(
            type=Ticket.TYPE_PROJECT,
            title="Project Beta",
            status=Ticket.STATUS_HANDOVER,
            assigned_to=self.engineer
        )
        valid, _, err = workflow.validate_status_transition(ticket, Ticket.STATUS_CLOSED, self.engineer)
        self.assertFalse(valid)
        self.assertIn("Warranty", err)

        TicketFile.objects.create(ticket=ticket, category=TicketFile.CATEGORY_WARRANTY, original_name="warranty.pdf")
        valid, _, _ = workflow.validate_status_transition(ticket, Ticket.STATUS_CLOSED, self.engineer)
        self.assertTrue(valid)

    def test_service_ticket_completion_and_receipt_rules(self):
        """Service ticket requires Site Report. If parts_purchased=True, also requires Receipt."""
        ticket = Ticket.objects.create(
            type=Ticket.TYPE_SERVICE,
            title="Service Bravo",
            status=Ticket.STATUS_IN_PROGRESS,
            parts_purchased=True,
            assigned_to=self.engineer
        )

        # Fails both
        valid, _, err = workflow.validate_status_transition(ticket, Ticket.STATUS_COMPLETED, self.engineer)
        self.assertFalse(valid)
        self.assertIn("Site Report", err)
        self.assertIn("Receipt", err)

        TicketFile.objects.create(ticket=ticket, category=TicketFile.CATEGORY_SITE_REPORT, original_name="sr.pdf")
        valid, _, err = workflow.validate_status_transition(ticket, Ticket.STATUS_COMPLETED, self.engineer)
        self.assertFalse(valid)
        self.assertIn("Receipt", err)

        TicketFile.objects.create(ticket=ticket, category=TicketFile.CATEGORY_RECEIPT, original_name="receipt.pdf")
        valid, _, _ = workflow.validate_status_transition(ticket, Ticket.STATUS_COMPLETED, self.engineer)
        self.assertTrue(valid)

    def test_super_admin_override(self):
        """Super admin can override missing prerequisites with a mandatory reason."""
        ticket = Ticket.objects.create(
            type=Ticket.TYPE_PROJECT,
            title="Project Gamma",
            status=Ticket.STATUS_INSTALLATION,
            assigned_to=self.engineer
        )

        # Without override reason -> fails
        valid, is_override, err = workflow.validate_status_transition(ticket, Ticket.STATUS_HANDOVER, self.admin, override_reason="")
        self.assertFalse(valid)

        # With override reason -> succeeds
        valid, is_override, err = workflow.validate_status_transition(
            ticket, Ticket.STATUS_HANDOVER, self.admin, override_reason="Customer waived formal site report on site"
        )
        self.assertTrue(valid)
        self.assertTrue(is_override)

        # Perform change
        workflow.change_ticket_status(
            ticket=ticket,
            new_status=Ticket.STATUS_HANDOVER,
            user=self.admin,
            override_reason="Customer waived formal site report on site"
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_HANDOVER)

        event = ticket.events.first()
        self.assertEqual(event.action, "STATUS_OVERRIDE")
        self.assertIn("Customer waived formal site report", event.note)


class SecurityAndSafetyTests(TestCase):
    def test_ssrf_blocked_ips(self):
        """SSRF protection blocks localhost, private IPs, and link-local metadata IPs."""
        blocked_urls = [
            "http://127.0.0.1/evil",
            "http://localhost:8000/test",
            "http://192.168.1.100/config",
            "http://10.0.0.5/api",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://files.example.com/test",
        ]
        for url in blocked_urls:
            with self.assertRaises(ValidationError):
                file_handler.validate_url_security(url)

    def test_safe_zip_extraction(self):
        """ZIP extraction detects duplicate SHA256 and blocks directory traversal attempts."""
        ticket = Ticket.objects.create(title="Zip Test Job")

        # Create an in-memory ZIP with normal files and a traversal attempt
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("test_report.pdf", b"%PDF-1.4 test report content")
            zf.writestr("photo1.jpg", b"fake jpeg data")
            zf.writestr("../evil_escape.sh", b"malicious payload")

        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        temp_zip.write(zip_buf.getvalue())
        temp_zip.close()

        try:
            created_files = file_handler.extract_zip_safely(temp_zip.name, ticket, TicketFile.CATEGORY_SITE_REPORT)
            # The traversal file must have been skipped
            self.assertEqual(len(created_files), 2)

            # Check categories: image went to 02 Photos, pdf went to 03 Site Reports
            photo = next(f for f in created_files if f.original_name == "photo1.jpg")
            pdf = next(f for f in created_files if f.original_name == "test_report.pdf")
            self.assertEqual(photo.category, TicketFile.CATEGORY_PHOTO)
            self.assertEqual(pdf.category, TicketFile.CATEGORY_SITE_REPORT)

            # Test duplicate skipping
            second_extract = file_handler.extract_zip_safely(temp_zip.name, ticket, TicketFile.CATEGORY_SITE_REPORT)
            self.assertEqual(len(second_extract), 0)  # All duplicates skipped
        finally:
            if os.path.exists(temp_zip.name):
                os.remove(temp_zip.name)


class PermissionsAndSoftDeleteTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(email="su@flowforce.com", full_name="Super Admin", role="SUPER_ADMIN", is_superuser=True)
        self.mgr_group, _ = Group.objects.get_or_create(name="Ticket Managers")
        self.manager = User.objects.create_user(email="mgr@flowforce.com", full_name="Ticket Manager", role="EMPLOYEE")
        self.manager.groups.add(self.mgr_group)

        self.engineer = User.objects.create_user(email="eng@flowforce.com", full_name="Engineer User", role="EMPLOYEE")
        self.outsider = User.objects.create_user(email="out@flowforce.com", full_name="Outsider User", role="EMPLOYEE")

    def test_permission_matrix(self):
        ticket = Ticket.objects.create(title="Permissions Test", assigned_to=self.engineer, created_by=self.engineer)

        # View: all active users
        self.assertTrue(workflow.can_view(self.outsider, ticket))
        self.assertTrue(workflow.can_view(self.engineer, ticket))

        # Edit: assignee can edit, outsider cannot
        self.assertTrue(workflow.can_edit_ticket(self.engineer, ticket))
        self.assertFalse(workflow.can_edit_ticket(self.outsider, ticket))
        self.assertTrue(workflow.can_edit_ticket(self.manager, ticket))
        self.assertTrue(workflow.can_edit_ticket(self.superadmin, ticket))

        # Delete: only managers / superadmin
        self.assertFalse(workflow.can_delete_ticket(self.engineer, ticket))
        self.assertFalse(workflow.can_delete_ticket(self.outsider, ticket))
        self.assertTrue(workflow.can_delete_ticket(self.manager, ticket))
        self.assertTrue(workflow.can_delete_ticket(self.superadmin, ticket))

    def test_soft_delete(self):
        ticket = Ticket.objects.create(title="To Delete", assigned_to=self.engineer)
        workflow.soft_delete_ticket(ticket, self.superadmin, reason="Duplicate ticket created")
        ticket.refresh_from_db()
        self.assertTrue(ticket.is_deleted)
        self.assertEqual(ticket.events.first().action, "DELETED")


class MCPToolsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="mcp@flowforce.com", full_name="MCP Admin", role="SUPER_ADMIN", is_superuser=True)

    def test_mcp_ticket_lifecycle(self):
        # Create
        created = mcp_tickets.ticket_create(
            type="PROJECT",
            title="MCP Test Project",
            customer_name="PT Petrochemical",
            job_no="2044",
            user_email="mcp@flowforce.com"
        )
        self.assertTrue(created.get("success"))
        t_id = created["id"]

        # Search
        search_res = mcp_tickets.tickets_search(query="2044")
        self.assertEqual(search_res["count"], 1)

        # Get
        details = mcp_tickets.ticket_get(ticket_id=t_id)
        self.assertEqual(details["customer_name"], "PT Petrochemical")

        # Add note
        note_res = mcp_tickets.ticket_add_note(ticket_id=t_id, note="Tested via MCP", user_email="mcp@flowforce.com")
        self.assertTrue(note_res.get("success"))

        # Advance status
        status_res = mcp_tickets.ticket_update_status(ticket_id=t_id, new_status="ORDERED_FROM_VENDOR", user_email="mcp@flowforce.com")
        self.assertTrue(status_res.get("success"))
        self.assertEqual(status_res["new_status"], "ORDERED_FROM_VENDOR")
