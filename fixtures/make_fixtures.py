"""Generate ALL synthetic fixtures deterministically (no real personal data).

Run:  python fixtures/make_fixtures.py
Creates fixtures/files/*.pdf (tiny valid PDFs), fixtures/claims.json (mock
portal seed) and fixtures/registry.json (our own policy-holder/consent registry).
"""
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "files")
os.makedirs(FILES, exist_ok=True)


def tiny_pdf(text: str) -> bytes:
    """Minimal valid one-page PDF containing `text` (deterministic bytes)."""
    safe = text.replace("\\", "").replace("(", "").replace(")", "")
    stream = f"BT /F1 12 Tf 50 750 Td ({safe}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def write(name: str, text: str) -> str:
    data = tiny_pdf(text)
    with open(os.path.join(FILES, name), "wb") as fh:
        fh.write(data)
    return hashlib.sha256(data).hexdigest()


def seeded(claim: str, doc_type: str) -> dict:
    name = f"{doc_type}_{claim}.pdf"
    return {"file_name": name, "doc_type": doc_type,
            "sha256": hashlib.sha256(tiny_pdf(f"SYNTHETIC {doc_type} for {claim} - demo only")).hexdigest()}


# ---- local files the family member has on their phone/laptop ---------------
for claim in ("CLM-1001", "CLM-2002", "CLM-4004", "CLM-4005"):
    write(f"final_invoice_{claim}.pdf", f"SYNTHETIC final_invoice for {claim} - demo only")
write("holiday_photo.pdf", "SYNTHETIC holiday photo - irrelevant to any claim")

# ---- portal seed ------------------------------------------------------------
DOCS = ["discharge_summary", "final_invoice", "id_proof"]
claims = [
    {"claim_no": "CLM-1001", "raw_status": "Documents Pending", "required_docs": DOCS,
     "present_docs": [seeded("CLM-1001", "discharge_summary"), seeded("CLM-1001", "id_proof")]},
    {"claim_no": "CLM-2002", "raw_status": "Documents Pending", "required_docs": DOCS,
     "present_docs": [seeded("CLM-2002", "discharge_summary"), seeded("CLM-2002", "id_proof")]},
    {"claim_no": "CLM-3003", "raw_status": "Under review", "required_docs": DOCS,
     "present_docs": [seeded("CLM-3003", d) for d in DOCS]},
    {"claim_no": "CLM-4004", "raw_status": "Documents Pending", "required_docs": DOCS,
     "present_docs": [seeded("CLM-4004", "discharge_summary"), seeded("CLM-4004", "id_proof")],
     "fault": {"mode": "timeout_after_commit", "times": 1}},
    {"claim_no": "CLM-4005", "raw_status": "Documents Pending", "required_docs": DOCS,
     "present_docs": [seeded("CLM-4005", "discharge_summary"), seeded("CLM-4005", "id_proof")],
     "fault": {"mode": "timeout_before_commit", "times": 1}},
    {"claim_no": "CLM-5006", "raw_status": "Pending Reconciliation", "required_docs": DOCS,
     "present_docs": [seeded("CLM-5006", d) for d in DOCS]},
    {"claim_no": "CLM-6006", "raw_status": "Approved", "required_docs": DOCS,
     "present_docs": [seeded("CLM-6006", d) for d in DOCS]},
    {"claim_no": "CLM-7007", "raw_status": "Rejected", "required_docs": DOCS,
     "present_docs": [seeded("CLM-7007", d) for d in DOCS]},
]
with open(os.path.join(HERE, "claims.json"), "w", encoding="utf-8") as fh:
    json.dump({"claims": claims}, fh, indent=2)

# ---- our own registry (policy holder + recorded consents) -------------------
registry = {
    "policies": {
        "POL-5001": {"claim_no": "CLM-1001", "holder": "Ramesh Kumar"},
        "POL-5002": {"claim_no": "CLM-2002", "holder": "Sunita Rao"},
        "POL-5003": {"claim_no": "CLM-3003", "holder": "Meera Nair"},
        "POL-5004": {"claim_no": "CLM-4004", "holder": "Gopal Iyer"},
        "POL-5005": {"claim_no": "CLM-4005", "holder": "Lakshmi Menon"},
        "POL-5006": {"claim_no": "CLM-5006", "holder": "Farid Khan"},
        "POL-5007": {"claim_no": "CLM-6006", "holder": "Asha Pillai"},
        "POL-5008": {"claim_no": "CLM-7007", "holder": "Vikram Shah"},
    },
    "consents": [
        {"claim_no": "CLM-1001", "grantee": "Priya Kumar", "granted_by": "Ramesh Kumar"},
        {"claim_no": "CLM-3003", "grantee": "Rohan Nair", "granted_by": "Meera Nair"},
        {"claim_no": "CLM-4004", "grantee": "Divya Iyer", "granted_by": "Gopal Iyer"},
        {"claim_no": "CLM-4005", "grantee": "Karthik Menon", "granted_by": "Lakshmi Menon"},
    ],
    "demo_scenarios": [
        {"label": "TC1 - Missing invoice (daughter, consent on file)", "claim_no": "CLM-1001",
         "policy_no": "POL-5001", "requester": "Priya Kumar", "consent": True,
         "question": "Where is my father's claim?"},
        {"label": "TC2 - Another family member's claim (no consent)", "claim_no": "CLM-2002",
         "policy_no": "POL-5002", "requester": "Anil Rao", "consent": True,
         "question": "Where is my aunt's claim?"},
        {"label": "TC3 - Claim 'Under review'", "claim_no": "CLM-3003",
         "policy_no": "POL-5003", "requester": "Rohan Nair", "consent": True,
         "question": "Has my mother's claim been approved yet?"},
        {"label": "TC4 - Upload will time out (after the file lands)", "claim_no": "CLM-4004",
         "policy_no": "POL-5004", "requester": "Divya Iyer", "consent": True,
         "question": "What is missing on my father's claim?"},
        {"label": "Extra - Upload times out BEFORE the file lands", "claim_no": "CLM-4005",
         "policy_no": "POL-5005", "requester": "Karthik Menon", "consent": True,
         "question": "What is missing on my mother's claim?"},
        {"label": "Extra - Unrecognised portal status (UNKNOWN)", "claim_no": "CLM-5006",
         "policy_no": "POL-5006", "requester": "Farid Khan", "consent": False,
         "question": "Where is my claim?"},
        {"label": "Extra - Approved claim", "claim_no": "CLM-6006",
         "policy_no": "POL-5007", "requester": "Asha Pillai", "consent": False,
         "question": "Where is my claim?"},
        {"label": "Extra - Rejected claim", "claim_no": "CLM-7007",
         "policy_no": "POL-5008", "requester": "Vikram Shah", "consent": False,
         "question": "Where is my claim?"},
    ],
}
with open(os.path.join(HERE, "registry.json"), "w", encoding="utf-8") as fh:
    json.dump(registry, fh, indent=2)
print("fixtures written")
