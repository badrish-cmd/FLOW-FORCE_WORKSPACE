import io
import shutil
import tempfile
from datetime import date, timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from auth_app.models import EmployeeUser
from employee_management.models import Department
from tables.models import CellValue, Column, Row, Table

from . import pid as pid_svc
from . import services
from .models import Attachment, ChecklistItem, Event, Team, WorkOrder


def png(name="photo.png", color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), color).save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


MEDIA = tempfile.mkdtemp(prefix="wo-test-")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=MEDIA)
class WorkOrderTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        dept = Department.objects.create(name="WO", slug="wo")
        mk = lambda e, role, n: EmployeeUser.objects.create_user(email=e, password="x", role=role, department=dept,
                                                                 full_name=n, status="APPROVED")
        self.gm = mk("umapathi@flow-force.com", "SUPER_ADMIN", "Umapathi")
        self.admin = mk("badrish@example.com", "ADMIN", "Badrish")
        self.creator = mk("creator@example.com", "EMPLOYEE", "Creator")
        self.eng1 = mk("eng1@example.com", "EMPLOYEE", "Engineer One")
        self.eng2 = mk("eng2@example.com", "EMPLOYEE", "Engineer Two")
        self.other = mk("other@example.com", "EMPLOYEE", "Other")
        services.set_access(self.creator, "CREATOR", self.admin)
        self.eng = Team.objects.get(code="ENG")
        self.wh = Team.objects.get(code="WH")
        self._make_pid_table()

    def _make_pid_table(self):
        t = Table.objects.create(name="LIST PID - PT FLOW FORCE INDONESIA", created_by=self.admin, job_type="LIST_PID")
        Table.objects.create(name="LIST PID - PT FLOW FORCE ENGINEERING INDONESIA", created_by=self.admin, job_type="LIST_PID")
        Column.objects.filter(table=t, name="PO").update(name="CUSTOMER PO")
        for i, name in enumerate(["DESCRIPTION_AS_QUOTATION", "DESCRIPTION_AS_PO", "UNIT_MEASUREMENT", "SUPPLY/PROJECT",
                                  "INVOICE_ADDRESS", "DELIVERY_ADDRESS"]):
            Column.objects.create(table=t, name=name, data_type="TEXT", position=30 + i)
        cols = {c.name: c for c in t.columns.all()}

        def row(vals):
            r = Row.objects.create(table=t, created_by=self.admin)
            for k, v in vals.items():
                CellValue.objects.create(row=r, column=cols[k], value=v, updated_by=self.admin)
            return r
        row({"S_NO": 4439, "PID": "1931", "CUSTOMER PO": "SCITKN/2611/0016", "COMPANY_NAME": "PT. SUPERSONIC",
             "DESCRIPTION_AS_PO": "Rebuild kit", "QTY": "2", "UNIT_MEASUREMENT": "unit"})
        row({"S_NO": 4440, "PID": "1932", "CUSTOMER PO": "5500625363", "COMPANY_NAME": "PT. CHEIL JEDANG INDONESIA",
             "ENQUIRY_NO/QUOTATION_NO": "0", "DATE": "2026-11-19", "DESCRIPTION_AS_PO": "2224617 - LANTERN RING",
             "QTY": "4", "UNIT_MEASUREMENT": "each", "DUE_DATE_FLOW_FORCE": "2026-12-20", "SUPPLY/PROJECT": "SUPPLY",
             "INVOICE_ADDRESS": "Desa Jati Gedong, Jombang", "DELIVERY_ADDRESS": "Jombang plant"})
        row({"S_NO": 4441, "PID": "1932", "CUSTOMER PO": "5500625363", "COMPANY_NAME": "PT. CHEIL JEDANG INDONESIA",
             "DESCRIPTION_AS_PO": "2224616 - O-RING", "QTY": "36", "UNIT_MEASUREMENT": "each"})
        row({"S_NO": 4442, "PID": "", "CUSTOMER PO": "5500625363", "COMPANY_NAME": "PT. CHEIL JEDANG INDONESIA",
             "DESCRIPTION_AS_PO": "2224585 - O-RING 60,5X2,4", "QTY": "8", "UNIT_MEASUREMENT": "each"})
        row({"S_NO": 4443, "PID": "", "CUSTOMER PO": "OTHER-PO", "DESCRIPTION_AS_PO": "Not ours", "QTY": "1"})
        self.table = t

    def login(self, u):
        self.client.force_login(u)

    def post_new(self, user=None, **extra):
        self.login(user or self.creator)
        data = {"pid": "1932", "team_id": self.eng.id, "title": "Seal kit check", "customer_name": "PT. CHEIL JEDANG INDONESIA",
                "customer_po": "5500625363", "job_no": "1808", "required_by": "2026-12-20",
                "line_desc": ["2224617 - LANTERN RING", "2224616 - O-RING"], "line_qty": ["4", "36"],
                "line_unit": ["each", "each"], "line_rate": ["", ""], "line_disc": ["", ""], "line_row": ["", ""],
                "stages": ["2"], "sections": ["customer_po", "sales_order", "items", "stages", "images", "signatures", "team"],
                "assignees": [self.eng1.id, self.eng2.id], "instructions": "Check seal sizes", "action": "save"}
        data.update(extra)
        return self.client.post(reverse("work_orders:create"), data)

    # ---------------------------------------------------------------- PID
    def test_load_pid_takes_following_lines_without_pid(self):
        head = pid_svc.load_pid("1932")
        self.assertEqual(head["customer_name"], "PT. CHEIL JEDANG INDONESIA")
        self.assertEqual(head["customer_po"], "5500625363")
        self.assertEqual(head["quotation_no"], "")          # "0" in LIST PID = none
        self.assertEqual(head["required_by"], date(2026, 12, 20))
        self.assertEqual([l["description"] for l in head["lines"]],
                         ["2224617 - LANTERN RING", "2224616 - O-RING", "2224585 - O-RING 60,5X2,4"])
        self.assertEqual(head["lines"][1]["qty"], 36)
        self.assertEqual(pid_svc.pid_table(), self.table)
        self.assertIsNone(pid_svc.load_pid("9999"))
        self.assertIn(("1932", "PT. CHEIL JEDANG INDONESIA"), pid_svc.recent_pids())

    def test_new_form_prefills_from_pid(self):
        self.login(self.creator)
        r = self.client.get(reverse("work_orders:create") + "?pid=1932&team=WH")
        self.assertContains(r, "2224585 - O-RING 60,5X2,4")
        self.assertContains(r, "Jombang plant")
        self.assertEqual(r.context["p"]["sections"], self.wh.default_sections)
        r = self.client.get(reverse("work_orders:create") + "?pid=9999")
        self.assertContains(r, "not found")

    # ---------------------------------------------------------------- access
    def test_access(self):
        self.login(self.other)
        self.assertEqual(self.client.get(reverse("work_orders:list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("work_orders:create")).status_code, 403)
        self.post_new()
        wo = WorkOrder.objects.get()
        self.login(self.other)
        self.assertEqual(self.client.get(reverse("work_orders:detail", args=[wo.id])).status_code, 404)
        self.login(self.eng1)   # assignee can't see a draft yet
        self.assertEqual(self.client.get(reverse("work_orders:detail", args=[wo.id])).status_code, 404)
        self.login(self.creator)
        self.assertEqual(self.client.get(reverse("work_orders:access")).status_code, 403)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("work_orders:access")).status_code, 200)

    # ---------------------------------------------------------------- draft
    def test_create_draft_and_delete(self):
        r = self.post_new(files=[png()], new_captions=["Seal photo"])
        wo = WorkOrder.objects.get()
        self.assertRedirects(r, reverse("work_orders:detail", args=[wo.id]), fetch_redirect_response=False)
        self.assertEqual(wo.status, WorkOrder.STATUS_DRAFT)
        self.assertIsNone(wo.number)
        self.assertEqual(wo.lines.count(), 2)
        self.assertEqual(set(wo.assignees.all()), {self.eng1, self.eng2})
        self.assertEqual(wo.checklist.filter(stage=2).count(), 4)
        self.assertEqual(wo.attachments.get().caption, "Seal photo")
        self.assertEqual(wo.pid_table, self.table)
        # preview PDF of the draft
        r = self.client.get(reverse("work_orders:pdf", args=[wo.id]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        # delete
        self.login(self.other)
        self.client.post(reverse("work_orders:delete", args=[wo.id]))
        self.assertTrue(WorkOrder.objects.exists())
        self.login(self.creator)
        self.client.post(reverse("work_orders:delete", args=[wo.id]))
        self.assertFalse(WorkOrder.objects.exists())
        self.assertFalse(Attachment.objects.exists())
        self.assertTrue(Event.objects.filter(action="deleted").exists())

    def test_submit_needs_assignee(self):
        self.post_new(assignees=[])
        wo = WorkOrder.objects.get()
        r = self.client.post(reverse("work_orders:submit", args=[wo.id]), follow=True)
        self.assertContains(r, "Pick at least one employee")
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_DRAFT)

    # ---------------------------------------------------------------- approval
    def _submitted(self, **extra):
        self.post_new(action="submit", **extra)
        return WorkOrder.objects.get(status=WorkOrder.STATUS_IN_REVIEW)

    def test_approve_numbers_and_emails_with_pdf_and_files(self):
        wo = self._submitted(files=[png("a.png"), SimpleUploadedFile("spec.xlsx", b"x" * 50)])
        self.assertTrue(mail.outbox == [] or all("approval" in m.subject.lower() for m in mail.outbox))
        self.login(self.creator)
        self.client.post(reverse("work_orders:approve", args=[wo.id]))
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_IN_REVIEW)      # creator can't approve
        self.login(self.gm)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("work_orders:approve", args=[wo.id]))
        wo.refresh_from_db()
        yy = date.today().year % 100
        self.assertEqual(wo.number, f"WO-{yy:02d}-1932-ENG-01")
        self.assertEqual(wo.status, WorkOrder.STATUS_ISSUED)
        self.assertTrue(wo.pdf.name.endswith(".pdf"))
        wo_mails = [m for m in mail.outbox if wo.number in m.subject]
        self.assertEqual(sorted(m.to[0] for m in wo_mails), ["eng1@example.com", "eng2@example.com"])
        names = [a[0] for a in wo_mails[0].attachments]
        self.assertIn(f"{wo.number}.pdf", names)
        self.assertIn("a.png", names)
        self.assertIn("spec.xlsx", names)
        # assignee now sees it
        self.login(self.eng1)
        self.assertEqual(self.client.get(reverse("work_orders:detail", args=[wo.id])).status_code, 200)
        r = self.client.get(reverse("work_orders:list"))
        self.assertContains(r, wo.number)

    def test_numbering_per_pid_team(self):
        yy = date.today().year % 100
        nums = []
        for team in (self.eng, self.eng, self.wh):
            self.post_new(action="submit", team_id=team.id)
            wo = WorkOrder.objects.filter(status=WorkOrder.STATUS_IN_REVIEW).latest("id")
            with self.captureOnCommitCallbacks(execute=True):
                services.approve(wo, self.gm)
            wo.refresh_from_db()
            nums.append(wo.number)
        self.assertEqual(nums, [f"WO-{yy:02d}-1932-ENG-01", f"WO-{yy:02d}-1932-ENG-02", f"WO-{yy:02d}-1932-WH-01"])

    def test_send_back_and_resubmit(self):
        wo = self._submitted()
        self.login(self.gm)
        r = self.client.post(reverse("work_orders:send_back", args=[wo.id]), {"comment": ""}, follow=True)
        self.assertContains(r, "Please write why")
        self.client.post(reverse("work_orders:send_back", args=[wo.id]), {"comment": "Add the drawing"})
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_SENT_BACK)
        self.assertIsNone(wo.number)
        self.login(self.creator)
        self.client.post(reverse("work_orders:submit", args=[wo.id]))
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_IN_REVIEW)

    # ---------------------------------------------------------------- team work
    def test_team_ticks_checklist_and_marks_done_then_close(self):
        wo = self._submitted()
        with self.captureOnCommitCallbacks(execute=True):
            services.approve(wo, self.gm)
        self.login(self.eng1)
        r = self.client.post(reverse("work_orders:status", args=[wo.id]), {"status": "DONE"}, follow=True)
        self.assertContains(r, "Tick all checklist items first")
        for it in wo.checklist.all():
            self.client.post(reverse("work_orders:tick", args=[wo.id, it.id]), {"done": "1"})
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_IN_PROGRESS)
        self.client.post(reverse("work_orders:progress", args=[wo.id]), {"text": "Seals measured", "files": [png("site.png")]})
        self.assertTrue(wo.attachments.filter(by_assignee=True).exists())
        self.client.post(reverse("work_orders:status", args=[wo.id]), {"status": "DONE"})
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_DONE)
        self.login(self.other)
        self.assertEqual(self.client.post(reverse("work_orders:status", args=[wo.id]), {"status": "CLOSED"}).status_code, 404)
        self.login(self.gm)
        self.client.post(reverse("work_orders:status", args=[wo.id]), {"status": "CLOSED"})
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_CLOSED)

    def test_revision(self):
        wo = self._submitted(files=[png()])
        with self.captureOnCommitCallbacks(execute=True):
            services.approve(wo, self.gm)
        wo.refresh_from_db()
        self.login(self.creator)
        r = self.client.post(reverse("work_orders:revise", args=[wo.id]))
        new = WorkOrder.objects.get(parent=wo)
        self.assertRedirects(r, reverse("work_orders:edit", args=[new.id]), fetch_redirect_response=False)
        self.assertEqual(new.status, WorkOrder.STATUS_DRAFT)
        self.assertEqual(new.lines.count(), 2)
        self.assertEqual(new.attachments.count(), 1)
        services.submit(new, self.creator)
        with self.captureOnCommitCallbacks(execute=True):
            services.approve(new, self.gm)
        new.refresh_from_db()
        wo.refresh_from_db()
        self.assertEqual(new.number, wo.number + "R1")
        self.assertEqual(wo.status, WorkOrder.STATUS_REVISED)

    # ---------------------------------------------------------------- PDF
    def test_pdf_all_sections_with_prices_and_images(self):
        from .models import SECTION_KEYS
        from .pdf import render_pdf
        self.post_new(sections=SECTION_KEYS, line_rate=["100000", "2500.5"], line_disc=["", "10"],
                      notes="Handle with care", files=[png("x.png")], stages=["2", "5", "10"])
        wo = WorkOrder.objects.get()
        data = render_pdf(wo)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 5000)
        wo.sections = []
        wo.save()
        self.assertTrue(render_pdf(wo).startswith(b"%PDF"))

    def test_pages_render(self):
        wo = self._submitted()
        for u in (self.gm, self.admin, self.creator):
            self.login(u)
            for url in (reverse("work_orders:list"), reverse("work_orders:detail", args=[wo.id]),
                        reverse("work_orders:list") + "?status=open&team=ENG&q=1932"):
                self.assertEqual(self.client.get(url).status_code, 200, (u.email, url))
        self.login(self.gm)
        self.assertContains(self.client.get(reverse("work_orders:detail", args=[wo.id])), "Approve & issue")


    def test_internal_work_order_without_pid(self):
        self.login(self.creator)
        r = self.client.get(reverse("work_orders:create") + "?pid=Factory cikarang")
        self.assertContains(r, "not found")
        self.assertContains(r, "internal work order")
        r = self.client.get(reverse("work_orders:create") + "?internal=1&ref=Factory Cikarang&team=WH")
        self.assertContains(r, "INTERNAL")
        self.assertContains(r, "Factory Cikarang")
        r = self.client.post(reverse("work_orders:create"), {
            "internal": "1", "pid": "INT", "reference": "", "team_id": self.wh.id, "line_desc": ["Shelf"],
            "line_qty": ["2"], "line_unit": ["pcs"], "line_rate": [""], "line_disc": [""], "line_row": [""],
            "assignees": [self.eng1.id], "action": "save"})
        self.assertFalse(WorkOrder.objects.exists())        # reference is required
        self.client.post(reverse("work_orders:create"), {
            "internal": "1", "pid": "INT", "reference": "Factory Cikarang - racks", "team_id": self.wh.id,
            "customer_name": "PT. Flow Force Indonesia", "line_desc": ["Shelf"], "line_qty": ["2"], "line_unit": ["pcs"],
            "line_rate": [""], "line_disc": [""], "line_row": [""], "sections": ["items", "team", "signatures"],
            "assignees": [self.eng1.id], "action": "submit"})
        wo = WorkOrder.objects.get()
        self.assertTrue(wo.is_internal)
        self.assertEqual(wo.pid, "INT")
        self.assertEqual(wo.reference, "Factory Cikarang - racks")
        with self.captureOnCommitCallbacks(execute=True):
            services.approve(wo, self.gm)
        wo.refresh_from_db()
        yy = date.today().year % 100
        self.assertEqual(wo.number, f"WO-{yy:02d}-INT-WH-01")
        self.assertTrue(wo.pdf.name.endswith(".pdf"))
        self.login(self.gm)
        self.assertContains(self.client.get(reverse("work_orders:list")), "Factory Cikarang - racks")

    def test_checklist_editable_and_pdf_options(self):
        from .pdf import render_pdf
        wo = self._submitted()
        self.login(self.creator)
        # waiting for approval: creator can't change it
        r = self.client.post(reverse("work_orders:checklist_add", args=[wo.id]), {"stage": "2", "text": "x"}, follow=True)
        self.assertContains(r, "can&#x27;t change the checklist")
        with self.captureOnCommitCallbacks(execute=True):
            services.approve(wo, self.gm)
        self.login(self.eng1)
        self.client.post(reverse("work_orders:checklist_add", args=[wo.id]),
                         {"stage": "2", "text": "Weld drawing for door slot", "assigned_to": self.eng2.id})
        item = wo.checklist.get(text="Weld drawing for door slot")
        self.assertEqual(item.assigned_to, self.eng2)
        first = wo.checklist.filter(stage=2).first()
        self.client.post(reverse("work_orders:checklist_edit", args=[wo.id, first.id]),
                         {"text": "Drawings prepared (SS container)", "assigned_to": self.eng1.id})
        first.refresh_from_db()
        self.assertEqual((first.text, first.assigned_to), ("Drawings prepared (SS container)", self.eng1))
        bom = wo.checklist.get(text="BOM prepared")
        self.client.post(reverse("work_orders:checklist_edit", args=[wo.id, bom.id]), {"delete": "1"})
        self.assertFalse(wo.checklist.filter(text="BOM prepared").exists())
        self.client.post(reverse("work_orders:checklist_add", args=[wo.id]), {"stage": "5", "text": "Pack container"})
        wo.refresh_from_db()
        self.assertIn(5, wo.stages)
        self.login(self.other)
        self.assertEqual(self.client.post(reverse("work_orders:checklist_add", args=[wo.id]), {"stage": "2", "text": "x"}).status_code, 404)
        # PDF: without qty/unit, in draft no "Approved by"
        wo.sections = ["items", "stages", "signatures"]
        wo.save()
        self.assertTrue(render_pdf(wo).startswith(b"%PDF"))
        wo.approved_at = None
        self.assertTrue(render_pdf(wo).startswith(b"%PDF"))
