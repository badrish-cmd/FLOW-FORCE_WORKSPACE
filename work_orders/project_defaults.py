"""Default stages, departments, tasks and checklist for the single (landscape) work order.

Everything here is only a starting point: the creator adds or removes stages, departments, tasks and
checklist points on each work order before sending it to the GM.
"""

TYPE_PROJECT = "PROJECT"
TYPE_SUPPLY = "SUPPLY"
TYPE_SERVICE = "SERVICE"
TYPES = [(TYPE_PROJECT, "Project"), (TYPE_SUPPLY, "Supply"), (TYPE_SERVICE, "Service")]
TYPE_NAMES = dict(TYPES)

# number, name, description, responsible teams, types it applies to
STAGES = [
    (1, "Sales Order / Customer PO", "Commercial and technical scope confirmation", ["ADM"],
     [TYPE_PROJECT, TYPE_SUPPLY, TYPE_SERVICE]),
    (2, "Engineering and Work Order Release", "Drawings, BOM, approvals and production instructions", ["ENG"],
     [TYPE_PROJECT, TYPE_SERVICE]),
    (3, "Procurement and Fabrication", "Material purchasing, manufacturing, assembly and quality checks", ["PRC", "WS"],
     [TYPE_PROJECT, TYPE_SUPPLY]),
    (4, "Factory Acceptance Testing (FAT)", "Equipment inspection, testing and release", ["ENG", "WS"], [TYPE_PROJECT]),
    (5, "Packing and Delivery", "Logistics, dispatch and customer receipt", ["WH"], [TYPE_PROJECT, TYPE_SUPPLY]),
    (6, "Site Installation / Supervision", "Mobilization, mechanical and electrical installation", ["SITE"],
     [TYPE_PROJECT, TYPE_SERVICE]),
    (7, "Pre-Commissioning and Site Acceptance Testing (SAT)",
     "Safety checks, functional testing and agreed acceptance verification", ["SITE", "ENG"], [TYPE_PROJECT]),
    (8, "Commissioning and Performance Trial", "Start-up, process testing and operational verification", ["ENG"],
     [TYPE_PROJECT]),
    (9, "Customer Handover", "Training, documentation and signed acceptance", ["ENG", "ADM"],
     [TYPE_PROJECT, TYPE_SERVICE]),
    (10, "Project Closure and Warranty", "Final costs, payments, outstanding items and after-sales support", ["ADM"],
     [TYPE_PROJECT, TYPE_SUPPLY, TYPE_SERVICE]),
]

# Department order on the work order
DEPT_ORDER = ["ENG", "PRC", "WS", "WH", "SITE", "ADM"]

# (text, types) - tasks of section 3
TASKS = {
    "ENG": [("Survey, drawings + BOM, customer approval", [TYPE_PROJECT, TYPE_SERVICE]),
            ("FAT & commissioning", [TYPE_PROJECT]),
            ("Service report", [TYPE_SERVICE]),
            ("As-built drawing & manual", [TYPE_PROJECT])],
    "PRC": [("Vendor POs issued", [TYPE_PROJECT, TYPE_SUPPLY]),
            ("Consumables & paint", [TYPE_PROJECT]),
            ("Material received & checked", [TYPE_PROJECT, TYPE_SUPPLY])],
    "WS": [("Cutting, welding, assembly", [TYPE_PROJECT]),
           ("Dimensional & weld inspection", [TYPE_PROJECT]),
           ("Painting", [TYPE_PROJECT])],
    "WH": [("Packing & packing list", [TYPE_PROJECT, TYPE_SUPPLY]),
           ("Transport to customer", [TYPE_PROJECT, TYPE_SUPPLY]),
           ("Delivery note signed by customer", [TYPE_PROJECT, TYPE_SUPPLY])],
    "SITE": [("Mobilisation & safety induction", [TYPE_PROJECT, TYPE_SERVICE]),
             ("Installation / service work at site", [TYPE_PROJECT, TYPE_SERVICE]),
             ("Pre-commissioning / SAT support", [TYPE_PROJECT])],
    "ADM": [("Sales order & job file", [TYPE_PROJECT, TYPE_SUPPLY, TYPE_SERVICE]),
            ("Delivery documents (DO / BAST)", [TYPE_PROJECT, TYPE_SUPPLY, TYPE_SERVICE]),
            ("Final invoice after handover", [TYPE_PROJECT, TYPE_SUPPLY, TYPE_SERVICE])],
}

# (text, types, file required) - quality & documents checklist (page 2, section C).
# Every checklist point needs a document number when ticked; certificates also need the file.
CHECKS = {
    "ENG": [("Drawings approved by customer", [TYPE_PROJECT, TYPE_SERVICE], True),
            ("BOM frozen / approved", [TYPE_PROJECT], False),
            ("Design calculation", [TYPE_PROJECT], False),
            ("ITP agreed with customer", [TYPE_PROJECT], False),
            ("SAT / commissioning report", [TYPE_PROJECT], True),
            ("As-built drawings + O&M manual", [TYPE_PROJECT], True)],
    "PRC": [("Material test certificates (MTC 3.1)", [TYPE_PROJECT, TYPE_SUPPLY], True),
            ("Vendor certificates (CE / ATEX / FDA)", [TYPE_PROJECT, TYPE_SUPPLY], True),
            ("Certificate of origin / import documents", [TYPE_PROJECT, TYPE_SUPPLY], False)],
    "WS": [("WPS / PQR - welding procedure", [TYPE_PROJECT], True),
           ("Welder qualification certificates", [TYPE_PROJECT], True),
           ("NDT reports (DPT / MPI / UT)", [TYPE_PROJECT], True),
           ("Dimensional inspection report", [TYPE_PROJECT], False),
           ("Paint DFT / coating report", [TYPE_PROJECT], False),
           ("FAT report signed", [TYPE_PROJECT], True)],
    "WH": [("Incoming inspection (GRN + QC)", [TYPE_PROJECT, TYPE_SUPPLY], False),
           ("Packing list + packing photos", [TYPE_PROJECT, TYPE_SUPPLY], False),
           ("Delivery order signed by customer", [TYPE_PROJECT, TYPE_SUPPLY], True)],
    "SITE": [("Safety: JSA / HIRA, permit, MSDS", [TYPE_PROJECT, TYPE_SERVICE], False),
             ("Installation / service report", [TYPE_PROJECT, TYPE_SERVICE], True),
             ("Training attendance record", [TYPE_PROJECT], False)],
    "ADM": [("Warranty certificate", [TYPE_PROJECT, TYPE_SUPPLY], True),
            ("BAST / handover signed", [TYPE_PROJECT, TYPE_SUPPLY, TYPE_SERVICE], True),
            ("Final invoice issued", [TYPE_PROJECT, TYPE_SUPPLY, TYPE_SERVICE], False)],
}

DEPTS_FOR_TYPE = {
    TYPE_PROJECT: DEPT_ORDER,
    TYPE_SUPPLY: ["PRC", "WH", "ADM"],
    TYPE_SERVICE: ["ENG", "SITE", "ADM"],
}

# Page 2, section A: parameters to start with
SPEC_PARAMETERS = ["Scope", "Material", "Dimensions", "Capacity / rating", "Surface finish", "Standards",
                   "Documents to hand over"]

FORM_NO = "FF-QF-WO-01"
FORM_REV = "00"
FORM_EFFECTIVE = "01/12/2026"


def stages_for(wo_type):
    return [s for s in STAGES if wo_type in s[4]]


def tasks_for(code, wo_type):
    return [t for t, types in TASKS.get(code, []) if wo_type in types]


def checks_for(code, wo_type):
    return [(t, f) for t, types, f in CHECKS.get(code, []) if wo_type in types]
