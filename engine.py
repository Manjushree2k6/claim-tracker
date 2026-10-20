"""engine.py - deterministic rules. CODE decides; the LLM never does.

status mapping, input validation, consent/authorisation, missing-document
calculation, file<->document mapping, idempotency key, message validation and
the independent Verifier (kept separate from the upload executor on purpose).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from contract import (QueryState, Status, VerificationResult, VerificationState)
from db import Database
from portal import PortalAdapter, PortalError, PortalTimeout

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# ---------------------------------------------------------------- status map
_STATUS_MAP: dict[str, Status] = {
    "approved": Status.APPROVED, "claim approved": Status.APPROVED,
    "under review": Status.UNDER_REVIEW, "in review": Status.UNDER_REVIEW,
    "under process": Status.UNDER_REVIEW, "processing": Status.UNDER_REVIEW,
    "rejected": Status.REJECTED, "claim rejected": Status.REJECTED, "denied": Status.REJECTED,
    "documents pending": Status.DOCS_MISSING, "docs pending": Status.DOCS_MISSING,
    "documents missing": Status.DOCS_MISSING, "additional documents required": Status.DOCS_MISSING,
}


def normalize_status(raw: Optional[str]) -> str:
    """Lower-case, treat '-'/'_' as spaces, drop only a trailing '.'/'!', collapse spaces.

    Intentionally NOT 'strip all punctuation': 'approved?' or 'approved (pending)'
    must stay unmapped (-> UNKNOWN) instead of silently becoming APPROVED.
    """
    s = (raw or "").lower().replace("-", " ").replace("_", " ").strip()
    s = re.sub(r"[.!]+$", "", s)
    return " ".join(s.split())


def map_status(raw: Optional[str]) -> Status:
    """EXACT-match mapping. Anything unmapped (or empty) is UNKNOWN (rule 2).

    Deliberately not substring based: 'not approved' or 'approval pending'
    must never turn into APPROVED.
    """
    return _STATUS_MAP.get(normalize_status(raw), Status.UNKNOWN)


# ------------------------------------------------------------ input checking
CLAIM_RE = re.compile(r"^CLM-\d{4,6}$")
POLICY_RE = re.compile(r"^POL-\d{4,6}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,59}$")


def validate_inputs(claim_no: str, policy_no: str, requester: str) -> list[str]:
    problems = []
    if not CLAIM_RE.match(claim_no or ""):
        problems.append("Claim number should look like CLM-1001.")
    if not POLICY_RE.match(policy_no or ""):
        problems.append("Policy number should look like POL-5001.")
    if not NAME_RE.match(requester or ""):
        problems.append("Please enter your full name (letters only).")
    return problems


# -------------------------------------------------------------- authorisation
def _norm_name(n: str) -> str:
    return " ".join((n or "").casefold().split())


@dataclass
class AuthDecision:
    authorized: bool
    state: QueryState
    rule: str
    reason: str


class Registry:
    """Our own policy-holder registry (NOT the insurer portal).

    Checking consent here means a blocked request causes zero portal actions.
    """

    def __init__(self, path: str | None = None) -> None:
        with open(path or os.path.join(FIXTURES, "registry.json"), encoding="utf-8") as fh:
            self.data = json.load(fh)

    @property
    def policies(self) -> dict:
        return self.data["policies"]

    @property
    def consents(self) -> list[dict]:
        return self.data["consents"]

    @property
    def demo_scenarios(self) -> list[dict]:
        return self.data["demo_scenarios"]

    def check_authorization(self, db: Database, claim_no: str, policy_no: str,
                           requester: str, consent_flag: bool) -> AuthDecision:
        """Rule 3 / section 9: a family relationship alone grants nothing."""
        rec = self.policies.get(policy_no)
        if not rec or rec["claim_no"] != claim_no:
            return AuthDecision(False, QueryState.UNCERTAIN, "R-unmatched",
                                "We could not match this claim number with this policy number. "
                                "Please check both numbers.")
        if _norm_name(requester) == _norm_name(rec["holder"]):
            return AuthDecision(True, QueryState.OK, "R3-requester-is-holder",
                                "Requester is the policy holder.")
        recorded = db.consent_for(claim_no, requester) or db.consent_for(
            claim_no, " ".join(requester.split()).title())
        if recorded and consent_flag:
            return AuthDecision(True, QueryState.OK, "R3-recorded-consent",
                                "A consent from the policy holder is on record for this requester.")
        if recorded and not consent_flag:
            why = ("A consent record exists for you, but you did not confirm it. "
                   "Tick the consent box to continue.")
        elif consent_flag:
            why = ("You ticked the consent box, but no consent from the policy holder is "
                   "recorded for you on this claim. Being a family member is not enough.")
        else:
            why = ("You are not the policy holder and there is no recorded consent from the "
                   "policy holder for you.")
        return AuthDecision(False, QueryState.CONSENT_REQUIRED, "R3-no-consent", why)


# ------------------------------------------------------- documents and files
DOC_LABELS = {
    "discharge_summary": "Discharge summary",
    "final_invoice": "Final hospital invoice",
    "id_proof": "ID proof",
}
DOC_KEYWORDS = {
    "final_invoice": ["invoice", "bill"],
    "discharge_summary": ["discharge"],
    "id_proof": ["id_proof", "idproof", "id-proof", "identity"],
}
_CLAIM_TAG = re.compile(r"clm[-_ ]?(\d+)", re.I)


def label(doc_type: str) -> str:
    return DOC_LABELS.get(doc_type, doc_type.replace("_", " ").capitalize())


def compute_missing(required: list[str], present_types: set[str]) -> list[str]:
    """Missing = required minus present. Order follows the portal's required list."""
    return [d for d in required if d not in present_types]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def list_available_files(files_dir: str | None = None) -> list[dict]:
    d = files_dir or os.path.join(FIXTURES, "files")
    out = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                out.append({"file_name": name, "path": p, "sha256": sha256_bytes(fh.read())})
    return out


def file_matches(doc_type: str, file_name: str, claim_no: str) -> bool:
    """Does this local file plausibly satisfy `doc_type` for `claim_no`?

    A file tagged with a DIFFERENT claim number is never a match (right-claim rule).
    """
    low = file_name.lower()
    tag = _CLAIM_TAG.search(file_name)
    if tag and f"CLM-{tag.group(1)}" != claim_no.upper():
        return False
    return any(k in low for k in DOC_KEYWORDS.get(doc_type, []))


def candidate_files(claim_no: str, missing: list[str], files: list[dict]) -> dict[str, list[dict]]:
    return {m: [f for f in files if file_matches(m, f["file_name"], claim_no)] for m in missing}


def idempotency_key(claim_no: str, sha256: str) -> str:
    """Rule 5: claim_id + file_sha256."""
    return f"{claim_no}:{sha256}"


# ----------------------------------------------------------- user-facing text
def next_step(status: Status, missing: list[str], candidates: dict[str, list[dict]],
              stamp: Optional[str]) -> str:
    if status == Status.DOCS_MISSING and missing:
        ready = [label(m) for m in missing if len(candidates.get(m, [])) == 1]
        unmatched = [label(m) for m in missing if len(candidates.get(m, [])) != 1]
        parts = []
        if ready:
            parts.append("Review and confirm the upload of: " + ", ".join(ready) + ".")
        if unmatched:
            parts.append("We have no single matching file for: " + ", ".join(unmatched) +
                         ". Please provide it.")
        return " ".join(parts)
    if status == Status.UNDER_REVIEW:
        return ("Nothing to do right now. The insurer is still reviewing and has not made a "
                "decision. Check again later.")
    if status == Status.APPROVED:
        return "The portal shows this claim as approved. Ask the insurer about the payment timeline."
    if status == Status.REJECTED:
        return "The portal shows a rejection. Ask the insurer for the rejection letter and appeal steps."
    return "Status could not be confirmed. Manual verification required: contact the insurer/TPA."


def template_message(status: Status, state: QueryState, missing: list[str], reason: str = "") -> str:
    """Deterministic plain-language message (also the LLM fallback)."""
    if state == QueryState.CONSENT_REQUIRED:
        return ("We can't show this claim yet. " + reason +
                " Ask the policy holder to give their consent, then try again.")
    if state == QueryState.UNCERTAIN and status == Status.UNKNOWN:
        return ("We are not sure about this claim's status, so we won't guess. " + reason +
                " Manual verification required.")
    if status == Status.DOCS_MISSING:
        miss = ", ".join(label(m) for m in missing) or "some documents"
        return f"The insurer is waiting for documents. Missing: {miss}. The claim cannot move forward until they arrive."
    if status == Status.UNDER_REVIEW:
        return "Your claim is under review. The insurer has not made a decision yet."
    if status == Status.APPROVED:
        return "The portal shows this claim as approved."
    if status == Status.REJECTED:
        return "The portal shows this claim as rejected."
    return "Status unknown. Manual verification required."


_APPROVAL_WORDS = re.compile(
    r"approv|sanction|accepted|cleared|settled|pay ?out|paid|good news|congrat", re.I)


def validate_message(message: str, status: Status, facts_text: str) -> tuple[bool, str]:
    """Guard for any LLM-written message (section 4 / rule 2).

    Reject when: too long; it uses approval-like wording for a non-approved
    status; it contains claim/policy IDs, file names or numbers that are not in
    the tool results.
    """
    if not message or len(message) > 700:
        return False, "empty or too long"
    if status != Status.APPROVED and _APPROVAL_WORDS.search(message):
        return False, f"approval-like wording used for status {status.value}"
    # whole-token comparison (a '3' must not be excused by the '3' inside 'CLM-3003')
    fact_ids = set(re.findall(r"\b(?:CLM|POL)-\d+\b|[\w\-]+\.pdf\b", facts_text))
    for tok in re.findall(r"\b(?:CLM|POL)-\d+\b|[\w\-]+\.pdf\b", message):
        if tok not in fact_ids:
            return False, f"unsupported identifier: {tok}"
    fact_nums = set(re.findall(r"\d+", facts_text))
    for num in re.findall(r"\d+", message):
        if num not in fact_nums:
            return False, f"unsupported number: {num}"
    return True, "ok"


# ------------------------------------------------------------------ Verifier
class Verifier:
    """Independent check. Shares NO code with the upload executor (rule 6).

    It re-reads the document list and status stamp from the portal and
    compares the observed state with the intended state:
        expected documents = documents before + the approved file (once)
    """

    def __init__(self, db: Database, portal: PortalAdapter) -> None:
        self.db, self.portal = db, portal

    def verify(self, claim_no: str, file_name: str) -> VerificationResult:
        prop = self.db.latest_proposal_for(claim_no, file_name)
        if not prop:
            res = VerificationResult(state=VerificationState.FAILED,
                                     reason="No stored proposal for this file and claim.")
            self.db.save_verification(claim_no, file_name, res)
            return res
        ex = self.db.latest_execution(prop["id"])
        if not ex or not ex["docs_before_json"]:
            res = VerificationResult(state=VerificationState.PENDING,
                                     reason="No execution recorded yet, nothing to verify.")
            self.db.save_verification(claim_no, file_name, res)
            return res
        before = json.loads(ex["docs_before_json"])
        try:
            after = self.portal.list_documents(claim_no)["documents"]
            stamp = self.portal.get_claim(claim_no)["status_stamp"]
        except (PortalTimeout, PortalError) as exc:
            res = VerificationResult(state=VerificationState.PENDING,
                                     docs_before=[d["file_name"] for d in before],
                                     reason=f"Could not re-read the portal ({exc}). Still pending.")
            self.db.save_verification(claim_no, file_name, res)
            return res
        sha = prop["sha256"]
        before_shas = [d["sha256"] for d in before]
        expected = before_shas + ([] if sha in before_shas else [sha])
        after_shas = [d["sha256"] for d in after]
        copies = after_shas.count(sha)
        if copies == 1 and Counter(after_shas) == Counter(expected):
            state, reason = VerificationState.VERIFIED, (
                "The approved file is on the claim exactly once and nothing else changed.")
            if ex["state"] in ("TIMEOUT", "FAILED"):
                reason += f" (Upload reported {ex['state']}, but the portal confirms the file.)"
        elif copies == 0:
            state, reason = VerificationState.FAILED, "The file is NOT in the portal's document list."
        elif copies > 1:
            state, reason = VerificationState.FAILED, f"DUPLICATE: {copies} copies of the file found."
        else:
            state, reason = VerificationState.FAILED, "Other documents changed unexpectedly."
        res = VerificationResult(state=state, docs_before=[d["file_name"] for d in before],
                                 docs_after=[d["file_name"] for d in after], status_stamp=stamp,
                                 reason=reason, copies_found=copies)
        self.db.save_verification(claim_no, file_name, res)
        return res
