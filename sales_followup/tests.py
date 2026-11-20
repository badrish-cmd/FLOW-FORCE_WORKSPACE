from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auth_app.models import EmployeeUser
from employee_management.models import Department
from tasks.models import EmailLog
from vendor_quotes.models import QuoteRevision, Vendor
from vendor_quotes.services import workflow, zoho

from . import services
from .models import FollowupEntry, SalesFollowup


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="")
class SalesFollowupTests(TestCase):
    def setUp(self):
        dept = Department.objects.create(name="SF", slug="sf")
        mk = lambda e, role, n: EmployeeUser.objects.create_user(email=e, password="x", role=role, department=dept,
                                                                 full_name=n, status="APPROVED")
        self.gm = mk("umapathi@flow-force.com", "SUPER_ADMIN", "Umapathi")
        self.admin = mk("ib@example.com", "ADMIN", "Badrish")
        self.chaithra = mk("chaithra@example.com", "EMPLOYEE", "Chaithra")
        self.mgr = mk("mgr@example.com", "EMPLOYEE", "Manager")
        self.out = mk("out@example.com", "EMPLOYEE", "Nobody")
        services.set_access(self.chaithra, "USER", self.admin)
        services.set_access(self.mgr, "MANAGER", self.admin)
        self.vendor = Vendor.objects.create(name="DMN Westinghouse", code="DMN", currency="EUR", default_exchange_rate=18000)
        self.quote, self.rev = workflow.create_quote(
            enquiry_number="ENQ/2026/10/2717", customer_name="PT Toyota Sunter 2", vendor=self.vendor,
            vendor_quote_no="X1", quote_file=SimpleUploadedFile("q.pdf", b"%PDF"), user=self.admin)
        QuoteRevision.objects.filter(pk=self.rev.pk).update(
            status=QuoteRevision.STATUS_APPROVED, decided_at=timezone.now() - timedelta(days=3),
            zoho_estimate_number="FFI_26000293")

    def login(self, u):
        self.client.force_login(u)

    def fu(self):
        services.sync_all()
        return SalesFollowup.objects.get(quote=self.quote)

    # ---------------------------------------------------------------- access
    def test_access(self):
        self.login(self.out)
        for name in ("list", "monitor", "access"):
            self.assertEqual(self.client.get(reverse(f"sales_followup:{name}")).status_code, 403)
        self.login(self.chaithra)
        r = self.client.get(reverse("sales_followup:list"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["can_use_sales_followup"])
        self.assertFalse(r.context["can_use_vendor_quotes"])
        self.assertEqual(self.client.get(reverse("sales_followup:monitor")).status_code, 403)
        self.assertEqual(self.client.get(reverse("sales_followup:access")).status_code, 403)
        self.login(self.mgr)
        self.assertEqual(self.client.get(reverse("sales_followup:monitor")).status_code, 200)
        self.assertEqual(self.client.get(reverse("sales_followup:access")).status_code, 403)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("sales_followup:access")).status_code, 200)

    # ---------------------------------------------------------------- list sync
    def test_old_approved_quotes_listed_without_prices(self):
        self.login(self.chaithra)
        r = self.client.get(reverse("sales_followup:list"))
        self.assertContains(r, "FFI_26000293")
        self.assertContains(r, "ENQ/2026/10/2717")
        fu = SalesFollowup.objects.get()
        self.assertEqual(fu.status, SalesFollowup.STATUS_WAITING)
        self.assertEqual(fu.next_followup_date, timezone.localtime(fu.approved_at).date() + timedelta(days=1))
        self.assertContains(r, "Overdue")
        r = self.client.get(reverse("sales_followup:detail", args=[fu.id]))
        self.assertNotContains(r, "IDR")
        self.assertNotContains(r, "Rp")

    def test_void_then_revised(self):
        fu = self.fu()
        QuoteRevision.objects.filter(pk=self.rev.pk).update(status=QuoteRevision.STATUS_VOID)
        self.assertEqual(self.fu().status, SalesFollowup.STATUS_CANCELLED)
        r1 = QuoteRevision.objects.create(quote=self.quote, number=1, status=QuoteRevision.STATUS_APPROVED,
                                          decided_at=timezone.now(), zoho_estimate_number="FFI_26000293R1")
        fu = self.fu()
        self.assertEqual(SalesFollowup.objects.count(), 1)
        self.assertEqual(fu.status, SalesFollowup.STATUS_WAITING)
        self.assertEqual(fu.zoho_number, "FFI_26000293R1")
        self.assertEqual(fu.revision, r1)

    def test_not_approved_not_listed(self):
        QuoteRevision.objects.filter(pk=self.rev.pk).update(status=QuoteRevision.STATUS_DRAFT)
        services.sync_all()
        self.assertFalse(SalesFollowup.objects.exists())

    # ---------------------------------------------------------------- entries
    def test_record_followup_and_default_next_day(self):
        fu = self.fu()
        self.login(self.chaithra)
        today = timezone.localdate()
        self.client.post(reverse("sales_followup:detail", args=[fu.id]), {
            "date": today.isoformat(), "contacted": "Esthie", "channel": "WHATSAPP",
            "update_text": "Customer reviewing with plant", "status": "REVIEWING", "next_date": ""})
        fu.refresh_from_db()
        self.assertEqual(fu.status, SalesFollowup.STATUS_REVIEWING)
        self.assertEqual(fu.next_followup_date, today + timedelta(days=1))
        self.assertEqual(fu.followup_count, 1)
        self.assertEqual(fu.last_followup_date, today)
        # explicit next date
        self.client.post(reverse("sales_followup:detail", args=[fu.id]), {
            "date": today.isoformat(), "contacted": "sales@flow-force.com", "channel": "EMAIL",
            "status": "FOLLOWED_UP", "next_date": (today + timedelta(days=5)).isoformat()})
        fu.refresh_from_db()
        self.assertEqual(fu.next_followup_date, today + timedelta(days=5))
        self.assertEqual(fu.followup_count, 2)
        # won closes it
        self.client.post(reverse("sales_followup:detail", args=[fu.id]), {
            "date": today.isoformat(), "contacted": "Esthie", "status": "WON", "update_text": "PO 123"})
        fu.refresh_from_db()
        self.assertEqual(fu.status, SalesFollowup.STATUS_WON)
        self.assertFalse(fu.is_open)
        self.assertEqual(EmailLog.objects.filter(subject__icontains="follow").count(), 0)   # nothing is mailed

    def test_entry_edit_delete_permissions(self):
        fu = self.fu()
        e = services.add_entry(fu, {"contacted": "Esthie", "status": "FOLLOWED_UP"}, self.chaithra)
        services.set_access(self.out, "USER", self.admin)
        self.login(self.out)
        self.assertEqual(self.client.post(reverse("sales_followup:entry_delete", args=[e.id])).status_code, 403)
        self.login(self.chaithra)
        self.client.post(reverse("sales_followup:entry_edit", args=[e.id]), {
            "date": timezone.localdate().isoformat(), "contacted": "Esthie Engela", "status": "REVIEWING"})
        e.refresh_from_db()
        self.assertEqual(e.contacted, "Esthie Engela")
        self.assertIsNotNone(e.edited_at)
        fu.refresh_from_db()
        self.assertEqual(fu.status, "REVIEWING")
        self.login(self.mgr)   # managers can correct anyone's entry
        self.client.post(reverse("sales_followup:entry_delete", args=[e.id]))
        self.assertFalse(FollowupEntry.objects.exists())
        fu.refresh_from_db()
        self.assertEqual(fu.status, SalesFollowup.STATUS_WAITING)
        self.assertEqual(fu.followup_count, 0)

    def test_needs_contact(self):
        fu = self.fu()
        with self.assertRaises(services.FollowupError):
            services.add_entry(fu, {"contacted": " ", "status": "FOLLOWED_UP"}, self.chaithra)

    # ---------------------------------------------------------------- CRM
    def test_deal_owner_from_crm_and_manual(self):
        fu = self.fu()
        with mock.patch.object(zoho, "crm_deal_owner", return_value={"owner": "Marketing", "deal_id": "47", "deal_name": "Filter"}) as m:
            self.assertTrue(services.refresh_owner(fu))
            m.assert_called_with("ENQ/2026/10/2717")
        fu.refresh_from_db()
        self.assertEqual(fu.deal_owner, "Marketing")
        self.login(self.chaithra)
        self.client.post(reverse("sales_followup:set_owner", args=[fu.id]), {"deal_owner": "Esthie"})
        fu.refresh_from_db()
        self.assertEqual(fu.deal_owner, "Esthie")
        self.assertTrue(fu.deal_owner_manual)
        with mock.patch.object(zoho, "crm_deal_owner") as m:
            services.refresh_owner(fu)
            m.assert_not_called()        # typed name isn't overwritten by the background refresh

    def test_crm_not_connected_message(self):
        fu = self.fu()
        with mock.patch.object(zoho, "crm_deal_owner", side_effect=zoho.ZohoError("OAUTH_SCOPE_MISMATCH")):
            self.assertFalse(services.refresh_owner(fu))
        fu.refresh_from_db()
        self.assertIn("Connect Zoho again", fu.crm_error)

    def test_monitor_counts(self):
        fu = self.fu()
        SalesFollowup.objects.filter(pk=fu.pk).update(deal_owner="Marketing")
        services.add_entry(fu, {"contacted": "x", "status": "FOLLOWED_UP"}, self.chaithra)
        self.login(self.mgr)
        r = self.client.get(reverse("sales_followup:monitor"))
        self.assertContains(r, "Marketing")
        self.assertContains(r, "Chaithra")
        self.assertEqual(r.context["totals"]["week_entries"], 1)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="",
                   VENDOR_QUOTE_APPROVED_EXTRA_EMAILS=["sourcing@flow-force.com", "automation@flow-force.com"])
class ApprovedEmailTests(TestCase):
    def test_approved_email_also_goes_to_sourcing_and_automation(self):
        dept = Department.objects.create(name="SF2", slug="sf2")
        gm = EmployeeUser.objects.create_user(email="umapathi@flow-force.com", password="x", role="SUPER_ADMIN",
                                              department=dept, full_name="Umapathi", status="APPROVED")
        ib = EmployeeUser.objects.create_user(email="ib@example.com", password="x", role="ADMIN",
                                              department=dept, full_name="Badrish", status="APPROVED")
        v = Vendor.objects.create(name="V", code="V", currency="EUR", default_exchange_rate=1)
        q, rev = workflow.create_quote(enquiry_number="ENQ/1", customer_name="C", vendor=v, vendor_quote_no="",
                                       quote_file=None, user=ib)
        from vendor_quotes.tests import priced_snapshot
        workflow.save_snapshot(rev, priced_snapshot([
            {"part": "Z1", "description": "SPRING", "qty": 6, "unit_price": 6, "sell": 163000}]), ib)
        rev.refresh_from_db()
        workflow.submit(rev, ib)
        rev.refresh_from_db()
        with mock.patch("vendor_quotes.tasks.run_approval_outputs.delay"), self.captureOnCommitCallbacks(execute=True):
            workflow.approve(rev, gm)
        to = sorted(EmailLog.objects.filter(subject__icontains="Quote approved").values_list("recipient_email", flat=True))
        self.assertEqual(to, ["automation@flow-force.com", "ib@example.com", "sourcing@flow-force.com"])
        self.assertTrue(SalesFollowup.objects.filter(quote=q).exists())
