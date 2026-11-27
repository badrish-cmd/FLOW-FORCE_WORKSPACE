"""Single (landscape) work order: one per PID for all departments, ticked in the workspace."""
import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auth_app.models import EmployeeUser
from employee_management.models import Department
from tasks.models import EmailLog

from . import project_services as ps
from . import services
from .models import Team, WODepartment, WOItem, WorkOrder, WorkOrderAccess

MEDIA = tempfile.mkdtemp(prefix="wo-proj-test-")
TICK = ["automation@flow-force.com", "operations@flow-force.com"]


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=MEDIA, WORK_ORDER_TICK_EMAILS=TICK)
class SingleWorkOrderTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        dept = Department.objects.create(name="WO", slug="wo")

        def mk(e, role, n, des=""):
            return EmployeeUser.objects.create_user(email=e, password="x", role=role, department=dept, full_name=n,
                                                    status="APPROVED", designation=des)
        self.gm = mk("umapathi@flow-force.com", "SUPER_ADMIN", "Umapathi", "General Manager")
        self.admin = mk("badrish@example.com", "ADMIN", "Badrish", "Admin Executive")
        self.creator = mk("creator@example.com", "EMPLOYEE", "Creator")
        self.spec = mk("spec@example.com", "EMPLOYEE", "Spec Editor")
        self.coord = mk("coord@example.com", "EMPLOYEE", "Coordinator")
        self.viewer = mk("viewer@example.com", "EMPLOYEE", "Viewer")
        self.andi = mk("andi@example.com", "EMPLOYEE", "Andi", "Engineering Manager")
        self.rudi = mk("rudi@example.com", "EMPLOYEE", "Rudi")
        self.sari = mk("sari@example.com", "EMPLOYEE", "Sari", "Procurement Officer")
        self.other = mk("other@example.com", "EMPLOYEE", "Other")
        services.set_access(self.creator, "CREATOR", self.admin)
        services.set_access(self.spec, "SPEC_EDITOR", self.admin)
        services.set_access(self.coord, "WORKER", self.admin)
        services.set_access(self.viewer, "VIEWER", self.admin)
        from .tests import WorkOrderTests
        WorkOrderTests._make_pid_table(self)

    def login(self, u):
        self.client.force_login(u)

    def create(self, wo_type="PROJECT"):
        self.login(self.creator)
        r = self.client.post(reverse("work_orders:create"), {
            "pid": "1932", "wo_type": wo_type, "customer_name": "PT. CHEIL JEDANG INDONESIA",
            "customer_contact": "Pak Bambang - Engineering Manager", "customer_po": "5500625363",
            "required_by": "2026-12-20", "line_desc": ["Support frame", "Installation"], "line_qty": ["1", "1"],
            "line_unit": ["lot", "lot"], "line_row": ["", ""], "instructions": "Weld by certified welders"})
        wo = WorkOrder.objects.latest("id")
        self.assertRedirects(r, reverse("work_orders:detail", args=[wo.id]))
        return wo

    def dept(self, wo, code):
        return wo.departments.get(team__code=code)

    def ready(self, wo):
        """Give every department a PIC; ENG gets Andi (PIC) + Rudi, PRC gets Sari."""
        for d in wo.departments.all():
            pic = {"ENG": self.andi, "PRC": self.sari}.get(d.team.code, self.admin)
            members = [self.rudi.id] if d.team.code == "ENG" else []
            ps.update_department(wo, self.creator, d.id, pic_id=pic.id, member_ids=members, target_date=None)

    def issue(self, wo):
        self.ready(wo)
        services.submit(wo, self.creator)
        with self.captureOnCommitCallbacks(execute=True):
            out = services.approve(wo, self.gm)
        wo.refresh_from_db()
        return out

    # ---------------------------------------------------------------- create / defaults
    def test_create_project_fills_defaults(self):
        wo = self.create()
        self.assertTrue(wo.is_project)
        self.assertEqual(wo.layout, WorkOrder.LAYOUT_PROJECT)
        self.assertEqual(wo.wo_stages.count(), 10)
        self.assertEqual([d.team.code for d in wo.departments.all()], ["ENG", "PRC", "WS", "WH", "SITE", "ADM"])
        eng = self.dept(wo, "ENG")
        self.assertIn("Drawings approved by customer", [i.text for i in eng.checks()])
        self.assertTrue(eng.items.get(text="Drawings approved by customer").file_required)
        self.assertEqual(wo.spec_rows.count(), 7)
        self.assertEqual(wo.lines.count(), 2)
        self.assertEqual(wo.customer_contact, "Pak Bambang - Engineering Manager")
        self.assertEqual(wo.pid_table_id, self.table.id)

    def test_supply_has_fewer_stages_and_departments(self):
        wo = self.create("SUPPLY")
        self.assertEqual([s.number for s in wo.wo_stages.all()], [1, 3, 5, 10])
        self.assertEqual([d.team.code for d in wo.departments.all()], ["PRC", "WH", "ADM"])

    def test_create_form_pages(self):
        self.login(self.creator)
        self.assertContains(self.client.get(reverse("work_orders:create")), "Pick the PID and the type")
        r = self.client.get(reverse("work_orders:create") + "?pid=1932")
        self.assertContains(r, "2224585 - O-RING 60,5X2,4")
        self.assertEqual(r.context["p"]["wo_type"], "SUPPLY")          # LIST PID says SUPPLY
        self.assertContains(self.client.get(reverse("work_orders:create") + "?internal=1&ref=Workshop"), "INTERNAL")
        self.login(self.other)
        self.assertEqual(self.client.get(reverse("work_orders:create")).status_code, 403)

    # ---------------------------------------------------------------- draft editing
    def test_draft_add_remove_stages_departments_items(self):
        wo = self.create()
        self.login(self.creator)
        st = wo.wo_stages.get(number=4)
        self.client.post(reverse("work_orders:stage_update", args=[wo.id, st.id]), {"delete": "1"})
        self.assertFalse(wo.wo_stages.filter(number=4).exists())
        self.client.post(reverse("work_orders:stage_add", args=[wo.id]), {"name": "Painting inspection", "teams": ["WS"],
                                                                          "target_date": "2026-12-10"})
        new = wo.wo_stages.get(name="Painting inspection")
        self.assertEqual(new.teams, ["WS"])
        site = self.dept(wo, "SITE")
        self.client.post(reverse("work_orders:dept_update", args=[wo.id, site.id]), {"delete": "1"})
        self.assertFalse(wo.departments.filter(team__code="SITE").exists())
        self.client.post(reverse("work_orders:dept_add", args=[wo.id]), {"team_id": Team.objects.get(code="SITE").id})
        self.assertTrue(wo.departments.filter(team__code="SITE").exists())
        eng = self.dept(wo, "ENG")
        self.client.post(reverse("work_orders:item_add", args=[wo.id, eng.id]),
                         {"kind": "CHECK", "text": "Hydro test report", "assigned_to": self.rudi.id, "file_required": "1"})
        it = eng.items.get(text="Hydro test report")
        self.assertTrue(it.is_check and it.file_required and it.assigned_to == self.rudi)
        self.client.post(reverse("work_orders:item_update", args=[wo.id, it.id]), {"delete": "1"})
        self.assertFalse(eng.items.filter(text="Hydro test report").exists())
        # PIC + members become assignees
        self.client.post(reverse("work_orders:dept_update", args=[wo.id, eng.id]),
                         {"pic": self.andi.id, "members": [self.rudi.id], "target_date": "2026-12-01"})
        self.assertEqual(set(wo.assignees.all()), {self.andi, self.rudi})

    def test_submit_needs_pic_for_every_department(self):
        wo = self.create()
        with self.assertRaisesMessage(services.WorkOrderError, "Pick the person in charge"):
            services.submit(wo, self.creator)
        self.ready(wo)
        services.submit(wo, self.creator)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_IN_REVIEW)
        # assignees can't see it until approved
        self.login(self.andi)
        self.assertEqual(self.client.get(reverse("work_orders:detail", args=[wo.id])).status_code, 404)

    def test_approve_numbers_pdf_and_issue_mail(self):
        wo = self.create()
        self.issue(wo)
        wo.refresh_from_db()
        yy = timezone.localdate().year % 100
        self.assertEqual(wo.number, f"WO-{yy:02d}-1932")
        self.assertEqual(wo.status, WorkOrder.STATUS_ISSUED)
        self.assertTrue(wo.pdf)
        sent = set(EmailLog.objects.filter(subject__contains=wo.number).values_list("recipient_email", flat=True))
        self.assertTrue({"andi@example.com", "rudi@example.com", "sari@example.com"} <= sent)
        # a second single WO for the same PID gets -2
        wo2 = self.create()
        self.issue(wo2)
        wo2.refresh_from_db()
        self.assertEqual(wo2.number, f"WO-{yy:02d}-1932-2")

    def test_pdf_renders_landscape_with_names_and_designations(self):
        wo = self.create()
        self.login(self.creator)
        r = self.client.get(reverse("work_orders:pdf", args=[wo.id]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(r.content))
        page = reader.pages[0]
        self.assertGreater(float(page.mediabox.width), float(page.mediabox.height))
        text = "".join(p.extract_text() for p in reader.pages)
        self.assertIn("WORK ORDER - SPECIFICATIONS", text)
        self.assertIn("FF-QF-WO-01", text)
        self.assertIn("of " + str(len(reader.pages)), text)
        self.assertNotIn("Approved by", text)                        # draft: no approver shown
        self.issue(wo)
        r = self.client.get(reverse("work_orders:pdf", args=[wo.id]))
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
        self.assertIn("Umapathi - General Manager", text)
        self.assertIn("Andi - Engineering Manager", text)

    # ---------------------------------------------------------------- ticking
    def test_tick_rules(self):
        wo = self.create()
        eng = self.dept(wo, "ENG")
        mine = ps.add_item(wo, self.creator, eng.id, kind="TASK", text="Rudi's own task", assigned_to_id=self.andi.id)
        self.issue(wo)
        task = eng.items.get(text="FAT & commissioning")
        drawing = eng.items.get(text="Drawings approved by customer")       # needs doc no. + file
        bom = eng.items.get(text="BOM frozen / approved")                  # needs doc no.
        # outsiders and viewers can't tick
        for u in (self.other, self.viewer, self.sari):
            with self.assertRaises(services.WorkOrderError):
                ps.tick_item(wo, u, task.id)
        # Rudi (member) can tick unassigned items, not items assigned to Andi
        with self.assertRaises(services.WorkOrderError):
            ps.tick_item(wo, self.rudi, mine.id)
        EmailLog.objects.all().delete()
        ps.tick_item(wo, self.rudi, task.id)
        task.refresh_from_db()
        self.assertEqual(task.state, WOItem.STATE_DONE)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_IN_PROGRESS)
        to = set(EmailLog.objects.values_list("recipient_email", flat=True))
        self.assertEqual(to, {"umapathi@flow-force.com", "andi@example.com"} | set(TICK))
        # PIC ticks the item assigned to him; checklist needs doc number; certificate needs the file
        ps.tick_item(wo, self.andi, mine.id)
        with self.assertRaisesMessage(services.WorkOrderError, "document number"):
            ps.tick_item(wo, self.andi, bom.id)
        ps.tick_item(wo, self.andi, bom.id, doc_ref="BOM-1932 R0")
        with self.assertRaisesMessage(services.WorkOrderError, "Attach"):
            ps.tick_item(wo, self.andi, drawing.id, doc_ref="FF-DWG-1932-01")
        ps.tick_item(wo, self.andi, drawing.id, doc_ref="FF-DWG-1932-01",
                     file=SimpleUploadedFile("approval.pdf", b"%PDF-1.4 test", content_type="application/pdf"))
        drawing.refresh_from_db()
        self.assertEqual(drawing.doc_ref, "FF-DWG-1932-01")
        self.assertTrue(drawing.attachment and drawing.attachment.by_assignee)
        # N/A needs a reason
        calc = eng.items.get(text="Design calculation")
        with self.assertRaisesMessage(services.WorkOrderError, "not applicable"):
            ps.na_item(wo, self.andi, calc.id, "")
        ps.na_item(wo, self.andi, calc.id, "Standard frame, no calculation")
        # untick: admin only
        with self.assertRaisesMessage(services.WorkOrderError, "Only an admin"):
            ps.reopen_item(wo, self.andi, calc.id)
        ps.reopen_item(wo, self.admin, calc.id)
        calc.refresh_from_db()
        self.assertEqual(calc.state, WOItem.STATE_OPEN)
        # coordinator (Worker level) can tick any department
        ps.tick_item(wo, self.coord, self.dept(wo, "PRC").items.get(text="Vendor POs issued").id)

    def test_tick_through_views_and_my_tasks(self):
        wo = self.create()
        self.issue(wo)
        eng = self.dept(wo, "ENG")
        bom = eng.items.get(text="BOM frozen / approved")
        self.login(self.rudi)
        r = self.client.get(reverse("work_orders:my_tasks"))
        self.assertContains(r, "BOM frozen / approved")
        self.assertNotContains(r, "Vendor POs issued")                   # other department
        r = self.client.post(reverse("work_orders:item_tick", args=[wo.id, bom.id]), {"doc_ref": "", "next": "my"})
        bom.refresh_from_db()
        self.assertEqual(bom.state, WOItem.STATE_OPEN)
        self.client.post(reverse("work_orders:item_tick", args=[wo.id, bom.id]), {"doc_ref": "BOM-R0", "next": "my"})
        bom.refresh_from_db()
        self.assertEqual(bom.state, WOItem.STATE_DONE)
        self.assertEqual(bom.done_by, self.rudi)
        r = self.client.get(reverse("work_orders:detail", args=[wo.id]))
        self.assertContains(r, "BOM-R0")
        self.assertContains(r, "Progress")

    def test_pic_adds_task_after_issue(self):
        wo = self.create()
        self.issue(wo)
        eng = self.dept(wo, "ENG")
        it = ps.add_item(wo, self.andi, eng.id, kind="TASK", text="Extra bracket", assigned_to_id=self.rudi.id)
        self.assertTrue(it.added_after_issue)
        with self.assertRaises(services.WorkOrderError):
            ps.add_item(wo, self.rudi, eng.id, kind="TASK", text="Not allowed")
        with self.assertRaises(services.WorkOrderError):   # structure changes after approval = revision
            ps.add_stage(wo, self.creator, {"name": "New stage"})

    def test_stage_marked_by_team_and_completion_then_gm_closes(self):
        wo = self.create("SERVICE")
        self.issue(wo)
        stage2 = wo.wo_stages.get(number=2)                 # ENG
        with self.assertRaises(services.WorkOrderError):
            ps.mark_stage(wo, self.sari, stage2.id)          # not in a department of that stage
        EmailLog.objects.all().delete()
        ps.mark_stage(wo, self.rudi, stage2.id)
        to = set(EmailLog.objects.filter(subject__contains="Stage 2 completed").values_list("recipient_email", flat=True))
        self.assertEqual(to, {"umapathi@flow-force.com", "creator@example.com", "andi@example.com"} | set(TICK))
        for st in wo.wo_stages.filter(done=False):
            ps.mark_stage(wo, self.admin, st.id)
        for it in WOItem.objects.filter(department__work_order=wo):
            if it.is_check:
                ps.tick_item(wo, self.admin, it.id, doc_ref="DOC-1",
                             file=SimpleUploadedFile("c.pdf", b"%PDF-1.4", content_type="application/pdf"))
            else:
                ps.tick_item(wo, self.admin, it.id)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_DONE)
        self.assertTrue(EmailLog.objects.filter(subject__contains="Completed - please close",
                                                recipient_email="umapathi@flow-force.com").exists())
        with self.assertRaises(services.WorkOrderError):
            services.set_status(wo, self.admin, WorkOrder.STATUS_CLOSED)   # only the GM closes
        # admin unticks -> back in progress, tick again -> completed
        any_item = WOItem.objects.filter(department__work_order=wo).first()
        ps.reopen_item(wo, self.admin, any_item.id)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_IN_PROGRESS)
        ps.na_item(wo, self.admin, any_item.id, "not needed")
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_DONE)
        services.set_status(wo, self.gm, WorkOrder.STATUS_CLOSED)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_CLOSED)

    # ---------------------------------------------------------------- page 2
    def test_spec_edit_confirm_and_lock(self):
        wo = self.create()
        self.login(self.creator)
        self.client.post(reverse("work_orders:spec_save", args=[wo.id]), {
            "spec_parameter": ["Material", "Finish"], "spec_requirement": ["Mild steel", "Painted"],
            "spec_final": ["SS400", "Painted"], "spec_reason": ["Customer asked", ""], "spec_changed": [],
            "doc_document": ["Customer spec"], "doc_reference": ["SPEC-1"], "doc_revision": ["0"], "doc_date": ["2026-11-02"],
            "doc_source": ["Customer"]})
        rows = list(wo.spec_rows.all())
        self.assertEqual([r.parameter for r in rows], ["Material", "Finish"])
        self.assertTrue(rows[0].changed)            # requirement != final
        self.assertFalse(rows[1].changed)
        self.assertEqual(wo.ref_docs.get().reference, "SPEC-1")
        self.issue(wo)
        wo.refresh_from_db()
        self.assertFalse(ps.can_edit_spec(self.andi, wo))             # no Spec editor level
        self.assertTrue(ps.can_edit_spec(self.spec, wo))
        with self.assertRaises(services.WorkOrderError):
            ps.confirm_spec(wo, self.andi, customer_by="Pak Bambang", customer_date=timezone.localdate(), method="email")
        with self.assertRaisesMessage(services.WorkOrderError, "Type who confirmed"):
            ps.confirm_spec(wo, self.spec, customer_by="", customer_date=None, method="")
        ps.confirm_spec(wo, self.spec, customer_by="Pak Bambang - Engineering Manager",
                        customer_date=timezone.localdate(), method="by email")
        wo.refresh_from_db()
        self.assertEqual(wo.spec_status, WorkOrder.SPEC_CONFIRMED)
        self.assertEqual(wo.spec_confirmed_by, self.spec)
        with self.assertRaises(services.WorkOrderError):
            ps.save_spec(wo, self.spec, [], [])
        from pypdf import PdfReader
        import io
        from .pdf_project import render_project_pdf
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(render_project_pdf(wo))).pages)
        self.assertIn("Pak Bambang - Engineering Manager", text)
        self.assertIn("Spec Editor", text)

    # ---------------------------------------------------------------- revision, delete, cancel
    def test_revision_keeps_ticks_and_goes_to_gm(self):
        wo = self.create()
        self.issue(wo)
        eng = self.dept(wo, "ENG")
        task = eng.items.get(text="FAT & commissioning")
        ps.tick_item(wo, self.andi, task.id)
        ps.mark_stage(wo, self.admin, wo.wo_stages.get(number=1).id)
        new = services.revise(wo, self.creator)
        self.assertEqual(new.revision, 1)
        self.assertEqual(new.status, WorkOrder.STATUS_DRAFT)
        self.assertEqual(new.spec_status, WorkOrder.SPEC_PENDING)
        self.assertEqual(new.departments.get(team__code="ENG").items.get(text="FAT & commissioning").state, WOItem.STATE_DONE)
        self.assertTrue(new.wo_stages.get(number=1).done)
        # ticked on the old one while the revision waits -> carried over at approval
        ps.tick_item(wo, self.andi, eng.items.get(text="As-built drawing & manual").id)
        ps.add_stage(new, self.creator, {"name": "Extra stage"})
        services.submit(new, self.creator)
        services.approve(new, self.gm)
        new.refresh_from_db()
        wo.refresh_from_db()
        self.assertEqual(new.number, f"{wo.number}R1")
        self.assertEqual(wo.status, WorkOrder.STATUS_REVISED)
        self.assertEqual(new.departments.get(team__code="ENG").items.get(text="As-built drawing & manual").state,
                         WOItem.STATE_DONE)

    def test_delete_draft_and_cancel_issued(self):
        wo = self.create()
        self.login(self.creator)
        self.client.post(reverse("work_orders:delete", args=[wo.id]))
        self.assertFalse(WorkOrder.objects.filter(pk=wo.pk).exists())
        wo = self.create()
        self.issue(wo)
        with self.assertRaises(services.WorkOrderError):
            services.delete_draft(wo, self.admin)
        services.set_status(wo, self.gm, WorkOrder.STATUS_CANCELLED)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WorkOrder.STATUS_CANCELLED)

    # ---------------------------------------------------------------- overdue, pages, access page
    def test_overdue_alerts_once_a_day(self):
        wo = self.create()
        eng = self.dept(wo, "ENG")
        ps.add_item(wo, self.creator, eng.id, kind="TASK", text="Late task", assigned_to_id=self.rudi.id,
                    target_date=timezone.localdate() - timedelta(days=2))
        self.issue(wo)
        EmailLog.objects.all().delete()
        self.assertEqual(ps.overdue_alerts(), 3)          # Rudi, Andi (PIC), creator
        self.assertEqual(set(EmailLog.objects.values_list("recipient_email", flat=True)),
                         {"rudi@example.com", "andi@example.com", "creator@example.com"})
        self.assertEqual(ps.overdue_alerts(), 0)          # not again the same day

    def test_pages_render(self):
        wo = self.create()
        for u in (self.creator, self.admin):
            self.login(u)
            r = self.client.get(reverse("work_orders:detail", args=[wo.id]))
            self.assertContains(r, "Project workflow")
            self.assertContains(r, "Quality & documents checklist")
            self.assertEqual(self.client.get(reverse("work_orders:edit", args=[wo.id])).status_code, 200)
        self.issue(wo)
        for u in (self.andi, self.gm, self.viewer, self.coord):
            self.login(u)
            self.assertEqual(self.client.get(reverse("work_orders:detail", args=[wo.id])).status_code, 200)
            self.assertEqual(self.client.get(reverse("work_orders:list")).status_code, 200)
            self.assertEqual(self.client.get(reverse("work_orders:my_tasks")).status_code, 200)
        self.login(self.other)
        self.assertEqual(self.client.get(reverse("work_orders:detail", args=[wo.id])).status_code, 404)

    def test_access_levels_and_designation(self):
        self.login(self.admin)
        self.client.post(reverse("work_orders:access"), {"user_id": self.other.id, "level": "SPEC_EDITOR"})
        self.assertEqual(WorkOrderAccess.objects.get(user=self.other).level, "SPEC_EDITOR")
        self.client.post(reverse("work_orders:access"), {"user_id": self.other.id, "set_designation": "1",
                                                         "designation": "Site Supervisor"})
        self.other.refresh_from_db()
        self.assertEqual(self.other.designation, "Site Supervisor")
        self.assertContains(self.client.get(reverse("work_orders:access")), "Spec editor")
