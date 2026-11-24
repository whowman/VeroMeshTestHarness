"""A healthcare organization: a health system with clinical, finance, HR, IT, data and
compliance functions. Shaped like the organizations VeroMesh sells to (a payer-provider
with a clinician copilot), with invented names throughout."""

NAME = "Kestrel Valley Health"
DOMAIN = "kestrelvalley.example"
CURRENCY = "USD"

# Org units: id, name, parent, cost centre, share of the headcount, the titles in it
# (the first is the head's), and the business roles its people fill by title.
UNITS = [
    ("EXEC", "Executive Office", None, "CC1000", 0.01, ["Chief Executive Officer", "Chief of Staff"]),
    ("CLIN", "Clinical Operations", "EXEC", "CC2000", 0.02, ["Chief Medical Officer", "Clinical Operations Manager"]),
    (
        "EMER",
        "Emergency Medicine",
        "CLIN",
        "CC2100",
        0.16,
        ["Chair of Emergency Medicine", "Attending Physician", "Registered Nurse", "Charge Nurse"],
    ),
    (
        "INTM",
        "Internal Medicine",
        "CLIN",
        "CC2200",
        0.18,
        ["Chair of Internal Medicine", "Attending Physician", "Registered Nurse", "Charge Nurse"],
    ),
    ("CARD", "Cardiology", "CLIN", "CC2300", 0.09, ["Chair of Cardiology", "Cardiologist", "Registered Nurse"]),
    ("PHAR", "Pharmacy", "CLIN", "CC2400", 0.05, ["Director of Pharmacy", "Pharmacist", "Pharmacy Technician"]),
    ("CINF", "Clinical Informatics", "CLIN", "CC2500", 0.02, ["Chief Medical Information Officer", "Clinical Informaticist"]),
    ("FIN", "Finance", "EXEC", "CC3000", 0.02, ["Chief Financial Officer", "Controller", "Financial Analyst"]),
    ("AP", "Accounts Payable", "FIN", "CC3100", 0.03, ["Accounts Payable Manager", "Accounts Payable Specialist"]),
    ("RCM", "Revenue Cycle", "FIN", "CC3200", 0.08, ["Director of Revenue Cycle", "Claims Specialist", "Coding Specialist"]),
    ("SUPP", "Supply Chain", "FIN", "CC3300", 0.04, ["Director of Supply Chain", "Procurement Manager", "Buyer"]),
    ("HR", "Human Resources", "EXEC", "CC4000", 0.03, ["Chief People Officer", "HR Business Partner", "HR Specialist"]),
    ("IT", "Information Technology", "EXEC", "CC5000", 0.02, ["Chief Information Officer", "IT Service Owner"]),
    ("INFRA", "Infrastructure", "IT", "CC5100", 0.04, ["Director of Infrastructure", "Systems Engineer", "Cloud Engineer"]),
    ("APPS", "Applications", "IT", "CC5200", 0.05, ["Director of Applications", "Application Analyst", "Integration Engineer"]),
    ("SEC", "Security", "IT", "CC5300", 0.02, ["Chief Information Security Officer", "Security Engineer", "Identity Engineer"]),
    ("DATA", "Data & Analytics", "EXEC", "CC6000", 0.04, ["Chief Data Officer", "Data Steward", "Data Engineer", "Analytics Engineer"]),
    ("COMP", "Compliance & Privacy", "EXEC", "CC7000", 0.02, ["Chief Compliance Officer", "Privacy Officer", "Compliance Analyst"]),
    ("NURS", "Nursing Administration", "CLIN", "CC2600", 0.08, ["Chief Nursing Officer", "Nurse Manager", "Registered Nurse"]),
]

# Titles to business roles (the roles authority and decision rights hang on).
TITLE_ROLES = {
    "Chief Executive Officer": ["Executive"],
    "Chief Medical Officer": ["Executive", "ClinicalLeader"],
    "Chief Financial Officer": ["Executive", "FinanceApprover"],
    "Chief Information Officer": ["Executive", "SystemOwner"],
    "Chief Information Security Officer": ["SecurityOfficer"],
    "Chief Data Officer": ["DataOwner"],
    "Chief Medical Information Officer": ["ClinicalLeader", "DataOwner"],
    "Chief Nursing Officer": ["ClinicalLeader"],
    "Chief Compliance Officer": ["ComplianceOfficer"],
    "Privacy Officer": ["PrivacyOfficer"],
    "Chief People Officer": ["HRLeader"],
    "Attending Physician": ["Clinician", "Prescriber"],
    "Cardiologist": ["Clinician", "Prescriber"],
    "Registered Nurse": ["Clinician"],
    "Charge Nurse": ["Clinician", "ChargeNurse"],
    "Nurse Manager": ["Clinician", "ClinicalManager"],
    "Pharmacist": ["Pharmacist"],
    "Chair of Emergency Medicine": ["Clinician", "ClinicalLeader"],
    "Chair of Internal Medicine": ["Clinician", "ClinicalLeader"],
    "Chair of Cardiology": ["Clinician", "ClinicalLeader"],
    "Director of Pharmacy": ["Pharmacist", "ClinicalLeader"],
    "Controller": ["FinanceApprover"],
    "Accounts Payable Manager": ["APManager"],
    "Accounts Payable Specialist": ["APClerk"],
    "Procurement Manager": ["Buyer", "ProcurementApprover"],
    "Buyer": ["Buyer"],
    "Data Steward": ["DataSteward"],
    "Data Engineer": ["DataEngineer"],
    "Analytics Engineer": ["DataEngineer"],
    "IT Service Owner": ["SystemOwner"],
    "Director of Applications": ["SystemOwner"],
    "Director of Infrastructure": ["SystemOwner"],
    "Application Analyst": ["TechnicalOwner"],
    "Systems Engineer": ["TechnicalOwner"],
    "Integration Engineer": ["TechnicalOwner"],
    "Identity Engineer": ["TechnicalOwner"],
    "HR Business Partner": ["HRPartner"],
}

ROLES = {
    "Executive": "Member of the executive team",
    "ClinicalLeader": "Leads a clinical service; accountable for its care protocols",
    "Clinician": "Delivers patient care",
    "Prescriber": "May prescribe and sign orders",
    "ChargeNurse": "Coordinates a unit's nursing for a shift",
    "ClinicalManager": "Manages a clinical team",
    "Pharmacist": "Verifies and dispenses medication",
    "FinanceApprover": "Approves spending above the operational limits",
    "APManager": "Approves supplier invoices",
    "APClerk": "Enters and matches supplier invoices",
    "Buyer": "Raises purchase orders",
    "ProcurementApprover": "Approves purchase orders",
    "DataOwner": "Accountable for a data domain",
    "DataSteward": "Maintains a data domain's definitions and quality",
    "DataEngineer": "Builds and runs data pipelines",
    "SystemOwner": "Business owner of a system",
    "TechnicalOwner": "Technical owner of a system",
    "SecurityOfficer": "Accountable for information security",
    "PrivacyOfficer": "Accountable for patient and employee privacy",
    "ComplianceOfficer": "Accountable for regulatory compliance",
    "HRLeader": "Accountable for people processes",
    "HRPartner": "Supports a function's people processes",
}

# Authorities: role, action, limit (USD, or None for no amount), co-approval above.
AUTHORITIES = [
    ("APClerk", "ApproveSupplierInvoice", 5_000, None),
    ("APManager", "ApproveSupplierInvoice", 50_000, 25_000),
    ("FinanceApprover", "ApproveSupplierInvoice", 1_000_000, 250_000),
    ("Buyer", "RaisePurchaseOrder", 10_000, None),
    ("ProcurementApprover", "RaisePurchaseOrder", 250_000, 100_000),
    ("Prescriber", "SignMedicationOrder", None, None),
    ("Pharmacist", "VerifyMedicationOrder", None, None),
    ("ClinicalLeader", "ApproveCareProtocolChange", None, None),
    ("PrivacyOfficer", "ApprovePatientDataRelease", None, None),
    ("SystemOwner", "ApproveSystemChange", None, None),
]

# Systems of record: id, name, kind, vendor, owning unit (business), technical unit,
# criticality, regulatory scope.
SYSTEMS = [
    ("EHR", "Epic EHR", "ehr", "Epic", "CINF", "APPS", "1 - most critical", ["HIPAA"]),
    ("HCM", "Workday HCM", "hr", "Workday", "HR", "APPS", "2 - somewhat critical", ["SOX"]),
    ("IDP", "Microsoft Entra ID", "identity", "Microsoft", "SEC", "SEC", "1 - most critical", ["HIPAA", "SOX"]),
    ("ITSM", "ServiceNow ITSM", "itsm", "ServiceNow", "IT", "APPS", "2 - somewhat critical", []),
    ("ERP", "Oracle Fusion ERP", "erp", "Oracle", "FIN", "APPS", "1 - most critical", ["SOX"]),
    ("LAKE", "Databricks Lakehouse", "data-platform", "Databricks", "DATA", "DATA", "2 - somewhat critical", ["HIPAA"]),
    ("SEM", "Stardog Knowledge Graph", "semantic", "Stardog", "DATA", "DATA", "3 - less critical", []),
    ("RCMS", "Revenue Cycle Platform", "rcm", "Waystar", "RCM", "APPS", "2 - somewhat critical", ["HIPAA"]),
    ("PHARM", "Pharmacy Information System", "pharmacy", "Omnicell", "PHAR", "APPS", "1 - most critical", ["HIPAA"]),
    ("M365", "Microsoft 365", "collaboration", "Microsoft", "IT", "INFRA", "3 - less critical", []),
]

# Data assets in the lakehouse: catalog.schema.table, layer, source system, owning
# unit, columns (name, type, classification).
ASSETS = [
    (
        "clinical.bronze.ehr_encounters",
        "bronze",
        "EHR",
        "CINF",
        [
            ("encounter_id", "string", "internal"),
            ("patient_mrn", "string", "phi"),
            ("admit_ts", "timestamp", "phi"),
            ("attending_npi", "string", "internal"),
            ("dx_codes", "array<string>", "phi"),
        ],
    ),
    (
        "clinical.bronze.ehr_medication_orders",
        "bronze",
        "EHR",
        "PHAR",
        [
            ("order_id", "string", "internal"),
            ("patient_mrn", "string", "phi"),
            ("drug_ndc", "string", "internal"),
            ("prescriber_npi", "string", "internal"),
            ("signed_ts", "timestamp", "phi"),
        ],
    ),
    (
        "clinical.silver.encounters",
        "silver",
        "EHR",
        "CINF",
        [
            ("encounter_id", "string", "internal"),
            ("individual_id", "string", "phi"),
            ("admit_date", "date", "phi"),
            ("department", "string", "internal"),
            ("primary_dx", "string", "phi"),
        ],
    ),
    (
        "clinical.silver.medication_orders",
        "silver",
        "EHR",
        "PHAR",
        [
            ("order_id", "string", "internal"),
            ("individual_id", "string", "phi"),
            ("drug", "string", "internal"),
            ("prescriber_id", "string", "internal"),
            ("signed_at", "timestamp", "phi"),
        ],
    ),
    (
        "clinical.gold.longitudinal_patient",
        "gold",
        "EHR",
        "CINF",
        [
            ("individual_id", "string", "phi"),
            ("encounters_12m", "int", "phi"),
            ("active_medications", "array<string>", "phi"),
            ("care_gaps", "array<string>", "phi"),
        ],
    ),
    (
        "finance.bronze.erp_invoices",
        "bronze",
        "ERP",
        "AP",
        [
            ("invoice_id", "string", "internal"),
            ("supplier_id", "string", "internal"),
            ("amount", "decimal(18,2)", "confidential"),
            ("due_date", "date", "internal"),
            ("approved_by", "string", "internal"),
        ],
    ),
    (
        "finance.silver.supplier_invoices",
        "silver",
        "ERP",
        "AP",
        [
            ("invoice_id", "string", "internal"),
            ("supplier", "string", "internal"),
            ("amount_usd", "decimal(18,2)", "confidential"),
            ("paid_on_time", "boolean", "internal"),
        ],
    ),
    ("finance.gold.on_time_payment", "gold", "ERP", "FIN", [("month", "date", "internal"), ("on_time_rate", "double", "internal")]),
    (
        "people.bronze.hcm_workers",
        "bronze",
        "HCM",
        "HR",
        [
            ("worker_id", "string", "pii"),
            ("legal_name", "string", "pii"),
            ("email", "string", "pii"),
            ("position_id", "string", "internal"),
            ("manager_id", "string", "pii"),
        ],
    ),
    (
        "people.silver.workforce",
        "silver",
        "HCM",
        "HR",
        [
            ("person_id", "string", "pii"),
            ("department", "string", "internal"),
            ("job_title", "string", "internal"),
            ("hire_date", "date", "pii"),
        ],
    ),
]
LINEAGE = [
    ("clinical.bronze.ehr_encounters", "clinical.silver.encounters"),
    ("clinical.bronze.ehr_medication_orders", "clinical.silver.medication_orders"),
    ("clinical.silver.encounters", "clinical.gold.longitudinal_patient"),
    ("clinical.silver.medication_orders", "clinical.gold.longitudinal_patient"),
    ("finance.bronze.erp_invoices", "finance.silver.supplier_invoices"),
    ("finance.silver.supplier_invoices", "finance.gold.on_time_payment"),
    ("people.bronze.hcm_workers", "people.silver.workforce"),
]

# KPIs: id, name, owning unit (its head owns it), unit of measure, the asset it is computed from.
KPIS = [
    ("OnTimePaymentRate", "Supplier invoices paid on time", "AP", "percent", "finance.gold.on_time_payment"),
    ("MedicationOrderVerificationTime", "Median time to verify a medication order", "PHAR", "minutes", "clinical.silver.medication_orders"),
    ("CareGapClosureRate", "Care gaps closed within 90 days", "CINF", "percent", "clinical.gold.longitudinal_patient"),
    ("EDLengthOfStay", "Median emergency department length of stay", "EMER", "hours", "clinical.silver.encounters"),
]

# AI agents: id, name, the title of the person each acts for, purpose.
AGENTS = [
    ("ClinicianCopilot", "Clinician Copilot", "Attending Physician", "Answers clinical questions and drafts orders for signature"),
    ("InvoiceAssistant", "Invoice Assistant", "Accounts Payable Specialist", "Matches and approves supplier invoices within limits"),
    ("ProcurementAgent", "Procurement Agent", "Buyer", "Raises purchase orders for stock replenishment"),
    ("ServiceDeskAgent", "Service Desk Agent", "Application Analyst", "Triages incidents and proposes changes"),
]

GIVEN = [
    "Ava",
    "Liam",
    "Maya",
    "Noah",
    "Zoe",
    "Ethan",
    "Iris",
    "Owen",
    "Leah",
    "Caleb",
    "Nora",
    "Isaac",
    "Ruby",
    "Miles",
    "Hana",
    "Jonah",
    "Elena",
    "Theo",
    "Priya",
    "Marcus",
    "Sofia",
    "Daniel",
    "Amara",
    "Lucas",
    "Grace",
    "Mateo",
    "Chloe",
    "Samuel",
    "Aisha",
    "Robert",
    "William",
    "Katherine",
    "Elizabeth",
    "Jennifer",
    "Michael",
    "Christopher",
]
FAMILY = [
    "Okafor",
    "Lindqvist",
    "Ramirez",
    "Chen",
    "Patel",
    "Novak",
    "Haddad",
    "Kowalski",
    "Nguyen",
    "Brennan",
    "Sato",
    "Mensah",
    "Fischer",
    "Alvarez",
    "Kim",
    "Duarte",
    "Ivanova",
    "Moreau",
    "Osei",
    "Larsen",
    "Romero",
    "Abara",
    "Whitfield",
    "Castellanos",
    "Petrov",
    "Yamamoto",
    "Delgado",
    "Achebe",
    "Sorensen",
    "Quinlan",
]
# Short forms people go by, and systems that pick them up.
NICKNAMES = {
    "Robert": "Bob",
    "William": "Will",
    "Katherine": "Kate",
    "Elizabeth": "Liz",
    "Jennifer": "Jen",
    "Michael": "Mike",
    "Christopher": "Chris",
    "Samuel": "Sam",
    "Daniel": "Dan",
}
LOCATIONS = ["Kestrel Valley Medical Center", "North Campus Clinic", "Riverside Outpatient Center", "Corporate Office"]

# ---------------------------------------------------------------- the Clinician Copilot slice
# Utilization management (prior authorization), the claims platform, claims and coverage
# data, the Copilot's decision rules, and the semantic layer's terms.

UNITS.append(
    (
        "UM",
        "Utilization Management",
        "CLIN",
        "CC2700",
        0.03,
        ["Medical Director of Utilization Management", "Utilization Review Nurse", "Prior Authorization Specialist"],
    )
)
TITLE_ROLES.update(
    {
        "Medical Director of Utilization Management": ["Clinician", "Prescriber", "ClinicalLeader", "UMDirector"],
        "Utilization Review Nurse": ["Clinician", "UMReviewer"],
        "Prior Authorization Specialist": ["PAIntake"],
    }
)
ROLES.update(
    {
        "UMDirector": "Physician who decides prior authorizations above the reviewers' limit, and every denial",
        "UMReviewer": "Nurse who approves prior authorizations within the review limit",
        "PAIntake": "Receives and prepares prior authorization requests",
    }
)
AUTHORITIES += [
    ("UMReviewer", "ApprovePriorAuthorization", 25_000, None),
    ("UMDirector", "ApprovePriorAuthorization", None, None),
    ("UMDirector", "DenyPriorAuthorization", None, None),
]
SYSTEMS.append(("CLAIMS", "Claims and Utilization Platform", "claims", "TriZetto", "UM", "APPS", "1 - most critical", ["HIPAA"]))
ASSETS += [
    (
        "claims.bronze.claims_837",
        "bronze",
        "CLAIMS",
        "RCM",
        [
            ("claim_id", "string", "internal"),
            ("member_id", "string", "phi"),
            ("provider_npi", "string", "internal"),
            ("service_date", "date", "phi"),
            ("cpt_codes", "array<string>", "phi"),
            ("billed_amount", "decimal(18,2)", "confidential"),
        ],
    ),
    (
        "claims.silver.claims",
        "silver",
        "CLAIMS",
        "RCM",
        [
            ("claim_id", "string", "internal"),
            ("individual_id", "string", "phi"),
            ("provider_id", "string", "internal"),
            ("service_date", "date", "phi"),
            ("allowed_amount", "decimal(18,2)", "confidential"),
            ("status", "string", "internal"),
        ],
    ),
    (
        "claims.silver.coverage",
        "silver",
        "CLAIMS",
        "UM",
        [
            ("individual_id", "string", "phi"),
            ("plan_id", "string", "internal"),
            ("coverage_start", "date", "phi"),
            ("coverage_end", "date", "phi"),
            ("prior_auth_required", "boolean", "internal"),
        ],
    ),
    (
        "claims.silver.prior_authorizations",
        "silver",
        "CLAIMS",
        "UM",
        [
            ("pa_id", "string", "internal"),
            ("individual_id", "string", "phi"),
            ("service_code", "string", "phi"),
            ("requested_amount", "decimal(18,2)", "confidential"),
            ("decision", "string", "internal"),
            ("decided_by", "string", "internal"),
            ("decided_at", "timestamp", "internal"),
        ],
    ),
]
LINEAGE += [
    ("claims.bronze.claims_837", "claims.silver.claims"),
    ("claims.silver.claims", "clinical.gold.longitudinal_patient"),
    ("claims.silver.coverage", "claims.silver.prior_authorizations"),
]
KPIS.append(("DaysPayableOutstanding", "Average days to pay suppliers", "FIN", "days", "finance.gold.on_time_payment"))

# Decision criteria: the objective cascade, with owners, priorities and the objectives that pull
# against each other. id, name, level, parent, owner role, priority, KPIs, conflicts with.
OBJECTIVES = [
    ("SoundFinances", "Keep the organization financially sound", "enterprise", None, "FinanceApprover", None, [], []),
    (
        "PaySuppliersOnTime",
        "Pay suppliers accurately and on time",
        "function",
        "SoundFinances",
        "APManager",
        1,
        ["OnTimePaymentRate"],
        ["PreserveWorkingCapital"],
    ),
    (
        "PreserveWorkingCapital",
        "Preserve working capital",
        "function",
        "SoundFinances",
        "FinanceApprover",
        2,
        ["DaysPayableOutstanding"],
        [],
    ),
]
# Capabilities and their health: id, name, owner role, processes, objectives served,
# (health KPI, threshold, better when).
CAPABILITIES = [
    ("AccountsPayable", "Accounts payable", "APManager", ["InvoiceApproval"], ["PaySuppliersOnTime"], ("OnTimePaymentRate", 95, "higher")),
]


def on_time_payment_rate(day: int) -> float:
    """Percent of supplier invoices paid on time. The AP Manager's post is vacant from day 20
    to 50; the backlog keeps the rate below its health threshold until day 95."""
    return 91.4 if 20 <= day < 95 else 97.2


KPIS.append(("PriorAuthTurnaround", "Median prior authorization decision time", "UM", "hours", "claims.silver.prior_authorizations"))

# What the Clinician Copilot may do, and who must act or review. ``act``: allow (the agent
# may), draft (it drafts; a person with the ``sign`` role signs), consult (the ``review``
# roles decide). ``limit``: an amount above which the ``above`` roles decide. ``care``: the
# requester must be treating the patient. ``owner``: the unit whose head owns the data or
# content the rule is about.
COPILOT_RULES = {
    "SummarizePatientHistory": {"act": "allow", "care": True, "owner": "CINF"},
    "IdentifyCareGap": {"act": "allow", "care": True, "owner": "CINF", "route": "generalPractitioner"},
    "CrossReferenceMedications": {"act": "allow", "care": True, "owner": "PHAR", "onMajorInteraction": ["Prescriber", "Pharmacist"]},
    "DraftMedicationOrder": {"act": "draft", "care": True, "sign": "Prescriber", "review": ["Pharmacist"], "owner": "PHAR"},
    "DraftHighAlertMedicationOrder": {
        "act": "draft",
        "care": True,
        "sign": "Prescriber",
        "review": ["Pharmacist", "Pharmacist"],
        "owner": "PHAR",
    },
    "RecommendDoseChange": {"act": "consult", "care": True, "review": ["Prescriber"], "owner": "PHAR"},
    "ApprovePriorAuthorization": {"act": "consult", "review": ["UMReviewer"], "limit": 25_000, "above": ["UMDirector"], "owner": "UM"},
    "DenyPriorAuthorization": {"act": "consult", "review": ["UMDirector"], "owner": "UM"},
    "ReleaseRecordsExternally": {"act": "consult", "review": ["PrivacyOfficer"], "owner": "COMP"},
    "ChangeCareProtocol": {"act": "consult", "review": ["ClinicalLeader"], "owner": "CINF"},
}
HIGH_ALERT = ["insulin glargine 100 units/mL", "warfarin 5 mg", "heparin 5,000 units"]
MAJOR_INTERACTIONS = [("warfarin 5 mg", "aspirin 81 mg"), ("lisinopril 10 mg", "spironolactone 25 mg")]

# The semantic layer: business terms, their definitions, the domain each belongs to, and
# the lakehouse tables (and columns) that hold them.
SEMANTIC_TERMS = [
    (
        "Individual",
        "Patient/Individual",
        "A unique person known to the organization, whatever their relationship to it",
        ["clinical.gold.longitudinal_patient.individual_id", "claims.silver.coverage.individual_id"],
    ),
    (
        "Patient",
        "Patient/Individual",
        "An individual in a care relationship with the organization",
        ["clinical.silver.encounters.individual_id"],
    ),
    ("Encounter", "Clinical Encounter", "An interaction between a patient and a provider for care", ["clinical.silver.encounters"]),
    ("MedicationOrder", "Clinical Encounter", "An order for a medication, signed by a prescriber", ["clinical.silver.medication_orders"]),
    ("Practitioner", "Provider", "A person licensed to deliver care", ["clinical.silver.medication_orders.prescriber_id"]),
    ("Provider", "Provider", "A practitioner or organization that bills for care", ["claims.silver.claims.provider_id"]),
    ("Diagnosis", "Terminology", "A coded clinical condition (ICD-10-CM)", ["clinical.silver.encounters.primary_dx"]),
    ("CareGap", "Terminology", "A recommended service a patient has not received", []),
    ("Claim", "Claims/Coverage", "A request for payment for services delivered", ["claims.silver.claims"]),
    ("Coverage", "Claims/Coverage", "The insurance plan an individual is enrolled in, and its dates", ["claims.silver.coverage"]),
    (
        "PriorAuthorization",
        "Claims/Coverage",
        "Approval required before a covered service is delivered",
        ["claims.silver.prior_authorizations"],
    ),
    (
        "SupplierInvoice",
        "Finance",
        "A supplier's bill for goods or services",
        ["finance.silver.supplier_invoices", "finance.bronze.erp_invoices.amount"],
    ),
]


# ---------------------------------------------------------------- decisions about people
# Workday business process rules (step 6b), as the custom report VeroMesh_Approval_Rules
# lays them out: process, rule type, security group, measure, limit (initiator) or the
# value above which a step applies (approval), organization scope, step order.
HR_RULES = [
    ("Request Compensation Change", "Initiator", "Manager", "Percent_Change", 5, "Supervisory Organization", None),
    ("Request Compensation Change", "Initiator", "HR Partner", "Percent_Change", 12, "Company", None),
    ("Request Compensation Change", "Approval", "Manager's Manager", "Percent_Change", 3, "", 1),
    ("Promote Employee Inbound", "Initiator", "Manager", "Grade_Steps", 1, "Supervisory Organization", None),
    ("Promote Employee Inbound", "Approval", "Manager's Manager", "", None, "", 1),
    ("Promote Employee Inbound", "Approval", "HR Partner", "", None, "", 2),
    ("Hire", "Initiator", "Manager", "Headcount", 2, "Organization", None),
    ("Hire", "Approval", "HR Partner", "", None, "", 1),
    ("Termination", "Initiator", "Manager", "", None, "Supervisory Organization", None),
    ("Termination", "Approval", "HR Partner", "", None, "", 1),
]
#: Business process types to VeroMesh actions, and Workday security groups to roles.
HR_ACTIONS = {
    "Request Compensation Change": "AdjustPay",
    "Promote Employee Inbound": "PromoteEmployee",
    "Hire": "HireEmployee",
    "Termination": "TerminateEmployee",
}
HR_GROUP_ROLES = {"Manager": "PeopleManager", "HR Partner": "HRPartner"}
ROLES["PeopleManager"] = "Manages people: has direct reports (Workday's Manager group)"
# An agent for a manager, chosen without the random draw (so the rest of the company is
# the same as before it was added): the first holder of this title.
HR_AGENT = (
    "PeopleAssistant",
    "People Assistant",
    "Director of Revenue Cycle",
    "Prepares pay changes, promotions, hires and terminations for a manager",
)
# Decisions about people touch the workforce: changes per day above which the HR leader is told.
CAPABILITIES.append(("WorkforceManagement", "Workforce management", "HRLeader", [], [], None))
HR_IMPACTS = [(action, "WorkforceManagement", 20, "changes per day") for action in HR_ACTIONS.values()]
