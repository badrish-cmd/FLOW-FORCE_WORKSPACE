import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from auth_app.models import EmployeeUser
from employee_management.models import Department
from tasks.models import EmailLog
from vendor_quotes.models import Vendor, VendorQuote

from . import services
from .models import AccessChange, QuoteFile, Remark, TrackerAccess, TrackerLog, VendorEnquiry

MEDIA = tempfile.mkdtemp(prefix="ve-test-")


def pdf(name="quote.pdf", body=b"%PDF-1.4 test"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=MEDIA, VENDOR_ENQUIRY_UPLOAD_EMAILS=["umapathi@flow-force.com"],
                   VENDOR_ENQUIRY_REMARK_EMAILS=["automation@flow-force.com"])
class TrackerTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        dept = Department.objects.create(name="VE Test", slug="ve-test")
        mk = lambda email, role, name: EmployeeUser.objects.create_user(
            email=email, password="x", role=role, department=dept, full_name=name, status="APPROVED")
        self.gm = mk("umapathi@flow-force.com", "SUPER_ADMIN", "Umapathi")
        self.admin = mk("automation@flow-force.com", "ADMIN", "Md Ibrahim")
        self.staff = mk("staff@example.com", "EMPLOYEE", "New Staff")
        self.other = mk("other@example.com", "EMPLOYEE", "Other Staff")
        self.outsider = mk("out@example.com", "EMPLOYEE", "No Access")
        self.manager = mk("mgr@example.com", "EMPLOYEE", "Manager Emp")
        services.set_access(self.staff, "USER", self.admin)
        services.set_access(self.other, "USER", self.admin)
        services.set_access(self.manager, "MANAGER", self.admin)
        self.vendor = Vendor.objects.create(name="DMN Westinghouse", code="DMN", currency="EUR", default_exchange_rate=18000)

    def login(self, user):
        self.client.force_login(user)

    def create(self, user=None, **extra):
        self.login(user or self.staff)
        data = {"enquiry_no": "enq/2026/11/600", "customer_name": "PT. Indofood", "vendor_name": "dmn westinghouse",
                "initial_enquiry": "2 x rotary valve", "sent_on": "2026-11-18", "status": "ENQUIRY_SENT",
                "vendor_quote_no": ""}
        data.update(extra)
        return self.client.post(reverse("vendor_enquiries:create"), data, follow=True)

    # ------------------------------------------------------------ access

    def test_no_access_user_is_blocked_everywhere(self):
        self.create()
        enq = VendorEnquiry.objects.get()
        self.login(self.outsider)
        urls = [reverse("vendor_enquiries:list"), reverse("vendor_enquiries:create"), reverse("vendor_enquiries:access"),
                reverse("vendor_enquiries:detail", args=[enq.id]), reverse("vendor_enquiries:edit", args=[enq.id])]
        for u in urls:
            self.assertEqual(self.client.get(u).status_code, 403, u)
        for u in [reverse("vendor_enquiries:delete", args=[enq.id]), reverse("vendor_enquiries:remark_add", args=[enq.id]),
                  reverse("vendor_enquiries:send_to_pricing", args=[enq.id])]:
            self.assertEqual(self.client.post(u, {"text": "x"}).status_code, 403, u)
        self.assertTrue(VendorEnquiry.objects.exists())

    def test_sidebar_shows_tracker_only_with_access(self):
        self.login(self.staff)
        r = self.client.get(reverse("vendor_enquiries:list"))
        self.assertContains(r, "Vendor Enquiry Tracker")
        self.assertTrue(r.context["can_use_vendor_enquiries"])
        self.assertFalse(r.context["can_use_vendor_quotes"])   # staff never sees Vendor Quotes / prices

    def test_user_sees_only_own_rows(self):
        self.create(self.staff)
        self.create(self.other, enquiry_no="ENQ/2026/11/601")
        mine = VendorEnquiry.objects.get(created_by=self.staff)
        theirs = VendorEnquiry.objects.get(created_by=self.other)
        self.login(self.staff)
        r = self.client.get(reverse("vendor_enquiries:list"))
        self.assertContains(r, mine.enquiry_no)
        self.assertNotContains(r, theirs.enquiry_no)
        self.assertEqual(self.client.get(reverse("vendor_enquiries:detail", args=[theirs.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("vendor_enquiries:delete", args=[theirs.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("vendor_enquiries:access")).status_code, 403)

    def test_manager_and_admin_see_all(self):
        self.create(self.staff)
        self.create(self.other, enquiry_no="ENQ/2026/11/601")
        for u in (self.manager, self.gm, self.admin):
            self.login(u)
            r = self.client.get(reverse("vendor_enquiries:list"))
            self.assertContains(r, "ENQ/2026/11/600")
            self.assertContains(r, "ENQ/2026/11/601")

    def test_team_access_page_admin_only_and_logged(self):
        self.login(self.manager)
        self.assertEqual(self.client.get(reverse("vendor_enquiries:access")).status_code, 403)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("vendor_enquiries:access")).status_code, 200)
        self.client.post(reverse("vendor_enquiries:access"), {"user_id": self.outsider.id, "level": "USER"})
        self.assertEqual(TrackerAccess.objects.get(user=self.outsider).level, "USER")
        self.client.post(reverse("vendor_enquiries:access"), {"user_id": self.outsider.id, "level": ""})
        self.assertFalse(TrackerAccess.objects.filter(user=self.outsider).exists())
        self.assertEqual(AccessChange.objects.filter(user=self.outsider).count(), 2)
        # an admin cannot be downgraded
        self.client.post(reverse("vendor_enquiries:access"), {"user_id": self.gm.id, "level": "USER"})
        self.assertFalse(TrackerAccess.objects.filter(user=self.gm).exists())

    # ------------------------------------------------------------ flow

    def test_create_matches_vendor_and_uppercases_enquiry(self):
        r = self.create()
        enq = VendorEnquiry.objects.get()
        self.assertEqual(enq.enquiry_no, "ENQ/2026/11/600")
        self.assertEqual(enq.vendor, self.vendor)
        self.assertEqual(enq.vendor_name, "DMN Westinghouse")
        self.assertEqual(enq.status, "ENQUIRY_SENT")
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertNotContains(r, "not found in Vendor Quotes, kindly create it")

    def test_new_vendor_warns(self):
        r = self.create(vendor_name="Brand New Valves")
        enq = VendorEnquiry.objects.get()
        self.assertIsNone(enq.vendor)
        self.assertContains(r, "not found in Vendor Quotes, kindly create it")

    def test_quote_got_requires_upload(self):
        r = self.create(status="QUOTE_GOT")
        self.assertFalse(VendorEnquiry.objects.exists())
        self.assertContains(r, "Please upload the vendor quote")

    def test_upload_sets_quote_got_and_emails_gm(self):
        self.create(status="PENDING_REPLY", quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        self.assertEqual(enq.status, "QUOTE_GOT")
        self.assertEqual(enq.files.count(), 1)
        mails = EmailLog.objects.all()
        self.assertEqual([m.recipient_email for m in mails], ["umapathi@flow-force.com"])
        self.assertIn("Vendor quote uploaded", mails[0].subject)
        self.assertIn("ENQ/2026/11/600", mails[0].body)

    def test_status_change_then_upload_via_edit(self):
        self.create()
        enq = VendorEnquiry.objects.get()
        url = reverse("vendor_enquiries:edit", args=[enq.id])
        base = {"enquiry_no": enq.enquiry_no, "customer_name": "PT. Indofood", "vendor_name": enq.vendor_name,
                "initial_enquiry": "", "sent_on": "2026-11-18", "vendor_quote_no": "Q-1"}
        self.client.post(url, {**base, "status": "PENDING_REPLY"})
        enq.refresh_from_db()
        self.assertEqual(enq.status, "PENDING_REPLY")
        self.client.post(url, {**base, "status": "QUOTE_GOT", "quote_file": pdf()})
        enq.refresh_from_db()
        self.assertEqual(enq.status, "QUOTE_GOT")
        self.assertEqual(EmailLog.objects.count(), 1)
        # other users cannot edit
        self.login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_revision_upload_emails_again(self):
        self.create(quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        r = self.client.post(reverse("vendor_enquiries:revise", args=[enq.id]),
                             {"quote_file": pdf("quote_r1.pdf"), "vendor_quote_no": "Q-1 R1"}, follow=True)
        self.assertContains(r, "Revision 1 uploaded")
        self.assertEqual(list(enq.files.values_list("revision", flat=True)), [1, 0])
        enq.refresh_from_db()
        self.assertEqual(enq.vendor_quote_no, "Q-1 R1")
        last = EmailLog.objects.order_by("-id").first()
        self.assertIn("Revised quote uploaded - Revision 1", last.subject)
        self.assertEqual(EmailLog.objects.count(), 2)

    def test_bad_file_type_rejected(self):
        r = self.create(quote_file=SimpleUploadedFile("x.exe", b"MZ"))
        self.assertFalse(VendorEnquiry.objects.exists())
        self.assertContains(r, "Please upload the vendor quote as PDF")

    def test_file_view_permissions(self):
        self.create(quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        qf = enq.files.get()
        url = reverse("vendor_enquiries:file", args=[enq.id, qf.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_delete_any_time_and_logged(self):
        self.create(quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        r = self.client.post(reverse("vendor_enquiries:delete", args=[enq.id]), follow=True)
        self.assertContains(r, "Deleted")
        self.assertFalse(VendorEnquiry.objects.exists())
        self.assertFalse(QuoteFile.objects.exists())
        self.assertTrue(TrackerLog.objects.filter(action="deleted", user=self.staff).exists())

    def test_manager_cannot_delete_others_row(self):
        self.create()
        enq = VendorEnquiry.objects.get()
        self.login(self.manager)
        self.assertEqual(self.client.post(reverse("vendor_enquiries:delete", args=[enq.id])).status_code, 403)

    # ------------------------------------------------------------ remarks

    def test_remark_emails_automation_in_capitals_and_reply_no_email(self):
        self.create(quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        EmailLog.objects.all().delete()
        self.login(self.staff)
        self.assertEqual(self.client.post(reverse("vendor_enquiries:remark_add", args=[enq.id]), {"text": "x"}).status_code, 403)
        self.login(self.gm)
        self.client.post(reverse("vendor_enquiries:remark_add", args=[enq.id]), {"text": "please confirm delivery time"})
        self.client.post(reverse("vendor_enquiries:remark_add", args=[enq.id]), {"text": "and the packing cost"})
        self.assertEqual(Remark.objects.filter(parent=None).count(), 2)
        mails = list(EmailLog.objects.order_by("id"))
        self.assertEqual([m.recipient_email for m in mails], ["automation@flow-force.com"] * 2)
        self.assertIn("PLEASE CONFIRM DELIVERY TIME", mails[0].body)
        self.assertIn("ENQ/2026/11/600", mails[0].body)
        remark = Remark.objects.filter(parent=None).first()
        self.login(self.staff)
        self.client.post(reverse("vendor_enquiries:remark_reply", args=[enq.id, remark.id]), {"text": "Asked vendor"})
        self.assertEqual(remark.replies.count(), 1)
        self.assertEqual(EmailLog.objects.count(), 2)   # replies don't email
        r = self.client.get(reverse("vendor_enquiries:detail", args=[enq.id]))
        self.assertContains(r, "Asked vendor")
        self.assertContains(r, "please confirm delivery time")
        self.login(self.other)
        self.assertEqual(self.client.post(reverse("vendor_enquiries:remark_reply", args=[enq.id, remark.id]), {"text": "x"}).status_code, 404)

    # ------------------------------------------------------------ price calculation

    def test_send_to_pricing_creates_vendor_quote_with_pdf(self):
        self.create(quote_file=pdf(), vendor_quote_no="20263557")
        enq = VendorEnquiry.objects.get()
        self.login(self.staff)
        self.assertEqual(self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id])).status_code, 403)
        self.login(self.gm)
        r = self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id]))
        quote = VendorQuote.objects.get()
        self.assertRedirects(r, reverse("vendor_quotes:quote_detail", args=[quote.id]), fetch_redirect_response=False)
        self.assertEqual(quote.vendor, self.vendor)
        self.assertEqual(quote.enquiry.number, "ENQ/2026/11/600")
        self.assertEqual(quote.vendor_quote_no, "20263557")
        self.assertEqual(quote.ai_status, VendorQuote.AI_NOT_STARTED)   # GM clicks Read with AI himself
        with quote.quote_file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 test")
        enq.refresh_from_db()
        self.assertEqual(enq.vendor_quote, quote)
        # staff still sees no price: Vendor Quotes is closed to her
        self.login(self.staff)
        self.assertEqual(self.client.get(reverse("vendor_quotes:quote_detail", args=[quote.id])).status_code, 403)
        self.assertContains(self.client.get(reverse("vendor_enquiries:detail", args=[enq.id])), "In price calculation")

    def test_send_revision_attaches_to_same_quote(self):
        self.create(quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        self.login(self.gm)
        self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id]))
        r = self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id]), follow=True)
        self.assertContains(r, "already in Price Calculation")
        self.login(self.staff)
        self.client.post(reverse("vendor_enquiries:revise", args=[enq.id]), {"quote_file": pdf("r1.pdf", b"%PDF R1")})
        self.login(self.gm)
        self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id]))
        self.assertEqual(VendorQuote.objects.count(), 1)
        quote = VendorQuote.objects.get()
        with quote.quote_file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF R1")

    def test_send_with_missing_vendor_offers_create(self):
        self.create(vendor_name="Brand New Valves", quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        self.login(self.gm)
        r = self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id]))
        self.assertContains(r, "Create vendor and send to Price Calculation")
        self.assertFalse(VendorQuote.objects.exists())
        self.assertFalse(Vendor.objects.filter(name="Brand New Valves").exists())
        r = self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id]), {"create_vendor": "1"}, follow=True)
        v = Vendor.objects.get(name="Brand New Valves")
        self.assertEqual(VendorQuote.objects.get().vendor, v)
        self.assertContains(r, "created with name only")

    def test_send_requires_quote(self):
        self.create()
        enq = VendorEnquiry.objects.get()
        self.login(self.gm)
        r = self.client.post(reverse("vendor_enquiries:send_to_pricing", args=[enq.id]), follow=True)
        self.assertContains(r, "must be uploaded")
        self.assertFalse(VendorQuote.objects.exists())

    def test_pages_render(self):
        self.create(quote_file=pdf())
        enq = VendorEnquiry.objects.get()
        for u in (self.staff, self.manager, self.gm):
            self.login(u)
            for url in (reverse("vendor_enquiries:list"), reverse("vendor_enquiries:list") + "?status=QUOTE_GOT&q=enq",
                        reverse("vendor_enquiries:detail", args=[enq.id]), reverse("vendor_enquiries:create")):
                self.assertEqual(self.client.get(url).status_code, 200, (u.email, url))
