"""Service-level tests: consent, approval gate, idempotency, independent verifier."""
from helpers import propose_tc1, raises, svc

from contract import ExecutionState, QueryState, Status, VerificationState
from service import NotAuthorized


def test_holder_can_read_and_status_comes_from_portal():
    s = svc()
    r = s.get_claim_status("CLM-6006", "POL-5007", "Asha Pillai", False)
    assert r.state == QueryState.OK and r.status == Status.APPROVED
    assert r.evidence and r.evidence[0].source_id_or_url.startswith("mock-portal://")


def test_unmapped_status_is_uncertain():
    s = svc()
    r = s.get_claim_status("CLM-5006", "POL-5006", "Farid Khan", False)
    assert r.state == QueryState.UNCERTAIN and r.status == Status.UNKNOWN
    assert "manual verification" in r.next_step.lower()


def test_rejected_and_input_errors():
    s = svc()
    assert s.get_claim_status("CLM-7007", "POL-5008", "Vikram Shah", False).status == Status.REJECTED
    s2 = svc()  # fresh portal log: invalid input must cause zero portal actions
    bad = s2.get_claim_status("hello", "world", "", True)
    assert bad.state == QueryState.UNCERTAIN and len(s2.portal.action_log()) == 0


def test_blocked_request_causes_zero_portal_actions():
    s = svc()
    r = s.get_claim_status("CLM-2002", "POL-5002", "Anil Rao", True)
    assert r.state == QueryState.CONSENT_REQUIRED and r.status == Status.UNKNOWN
    assert s.portal.action_log() == []
    with raises(NotAuthorized):
        s.propose_upload("CLM-2002", "final_invoice_CLM-2002.pdf")
    assert s.portal.action_log() == []


def test_unknown_claim_in_portal_is_uncertain():
    s = svc()
    s.portal.db.execute("DELETE FROM portal_claims WHERE claim_no='CLM-1001'")
    r = s.get_claim_status("CLM-1001", "POL-5001", "Ramesh Kumar", False)
    assert r.state == QueryState.UNCERTAIN and r.rule_fired == "R-portal-unavailable"


def test_inconsistent_portal_data_is_uncertain():
    s = svc()  # CLM-3003 has every doc; pretend portal says docs pending
    s.portal.db.execute("UPDATE portal_claims SET raw_status='Documents Pending' WHERE claim_no='CLM-3003'")
    r = s.get_claim_status("CLM-3003", "POL-5003", "Meera Nair", False)
    assert r.state == QueryState.UNCERTAIN and r.rule_fired == "R10-inconsistent"


def test_proposal_rules():
    s = svc()
    s.get_claim_status("CLM-1001", "POL-5001", "Priya Kumar", True)
    with raises(ValueError):
        s.propose_upload("CLM-1001", "final_invoice_CLM-2002.pdf")  # file for another claim
    with raises(ValueError):
        s.propose_upload("CLM-1001", "holiday_photo.pdf")  # matches nothing missing
    with raises(ValueError):
        s.propose_upload("CLM-1001", "../../etc/passwd")  # not one of the listed files
    p1 = s.propose_upload("CLM-1001", "final_invoice_CLM-1001.pdf")
    p2 = s.propose_upload("CLM-1001", "final_invoice_CLM-1001.pdf")
    assert p1.proposal_id == p2.proposal_id  # reruns don't spawn duplicates
    assert p1.destination_claim == "CLM-1001" and "cannot easily be removed" in p1.change_summary


def test_no_upload_without_approval_record():
    s = svc()
    p = propose_tc1(s)
    assert s.portal.upload_attempts("CLM-1001") == []  # proposing writes nothing
    r = s.execute_upload(p.proposal_id, False)
    assert r.state == ExecutionState.FAILED and s.portal.upload_attempts("CLM-1001") == []
    assert s.db.approvals_for(p.proposal_id)[0]["decision"] == "REJECTED"
    again = s.execute_upload(p.proposal_id, True)  # a rejected proposal stays rejected
    assert again.state == ExecutionState.FAILED and s.portal.upload_attempts("CLM-1001") == []
    assert s.execute_upload("PROP-DOESNOTEXIST", True).state == ExecutionState.FAILED


def test_approved_upload_stores_approval_before_write_and_verifies():
    s = svc()
    p = propose_tc1(s)
    r = s.execute_upload(p.proposal_id, True)
    assert r.state == ExecutionState.EXECUTED
    ap = s.db.approvals_for(p.proposal_id)
    assert len(ap) == 1 and ap[0]["file_sha256"] == p.sha256 and ap[0]["destination_claim"] == "CLM-1001"
    v = s.verify_upload("CLM-1001", p.file_name)
    assert v.state == VerificationState.VERIFIED and len(v.docs_after) == len(v.docs_before) + 1
    # portal moved the claim on once complete (documented mock assumption)
    after = s.get_claim_status("CLM-1001", "POL-5001", "Priya Kumar", True)
    assert after.status == Status.UNDER_REVIEW and after.missing_docs == []


def test_double_click_never_double_uploads():
    s = svc()
    p = propose_tc1(s)
    assert s.execute_upload(p.proposal_id, True).state == ExecutionState.EXECUTED
    assert s.execute_upload(p.proposal_id, True).state == ExecutionState.SKIPPED_DUPLICATE
    assert len(s.portal.upload_attempts("CLM-1001")) == 1
    docs = s.portal.list_documents("CLM-1001")["documents"]
    assert sum(1 for d in docs if d["sha256"] == p.sha256) == 1


def test_file_changed_after_approval_is_refused():
    import os, shutil, tempfile
    from service import ClaimService
    from db import Database
    tmp = tempfile.mkdtemp()
    src = os.path.join(os.path.dirname(__file__), "..", "fixtures", "files")
    for n in os.listdir(src):
        shutil.copy(os.path.join(src, n), tmp)
    s = ClaimService(db=Database(":memory:"), files_dir=tmp)
    s.get_claim_status("CLM-1001", "POL-5001", "Priya Kumar", True)
    p = s.propose_upload("CLM-1001", "final_invoice_CLM-1001.pdf")
    with open(os.path.join(tmp, "final_invoice_CLM-1001.pdf"), "ab") as fh:
        fh.write(b"tampered")
    r = s.execute_upload(p.proposal_id, True)
    assert r.state == ExecutionState.FAILED and s.portal.upload_attempts("CLM-1001") == []
    shutil.rmtree(tmp)


def test_timeout_after_commit_does_not_double_upload():
    s = svc()
    s.get_claim_status("CLM-4004", "POL-5004", "Divya Iyer", True)
    p = s.propose_upload("CLM-4004", "final_invoice_CLM-4004.pdf")
    r = s.execute_upload(p.proposal_id, True)
    assert r.state == ExecutionState.SKIPPED_DUPLICATE
    assert len(s.portal.upload_attempts("CLM-4004")) == 1
    assert s.verify_upload("CLM-4004", p.file_name).copies_found == 1


def test_timeout_before_commit_retries_once_same_key():
    s = svc()
    s.get_claim_status("CLM-4005", "POL-5005", "Karthik Menon", True)
    p = s.propose_upload("CLM-4005", "final_invoice_CLM-4005.pdf")
    r = s.execute_upload(p.proposal_id, True)
    assert r.state == ExecutionState.EXECUTED and r.attempts == 2
    ups = s.portal.upload_attempts("CLM-4005")
    assert len(ups) == 2 and all(p.idempotency_key in u["detail"] for u in ups)
    assert s.verify_upload("CLM-4005", p.file_name).state == VerificationState.VERIFIED


def test_repeated_timeouts_stop_after_one_retry():
    s = svc()
    s.portal.db.execute("UPDATE portal_faults SET remaining=5 WHERE claim_no='CLM-4005'")
    s.get_claim_status("CLM-4005", "POL-5005", "Karthik Menon", True)
    p = s.propose_upload("CLM-4005", "final_invoice_CLM-4005.pdf")
    r = s.execute_upload(p.proposal_id, True)
    assert r.state == ExecutionState.TIMEOUT and r.attempts == 2  # never a 3rd try
    assert len(s.portal.upload_attempts("CLM-4005")) == 2
    v = s.verify_upload("CLM-4005", p.file_name)
    assert v.state == VerificationState.FAILED and v.copies_found == 0  # failure stays visible


def test_verifier_detects_duplicates_and_missing():
    s = svc()
    p = propose_tc1(s)
    assert s.verify_upload("CLM-1001", p.file_name).state == VerificationState.PENDING  # not run yet
    s.execute_upload(p.proposal_id, True)
    s.portal.db.execute(  # inject a second copy behind our back
        "INSERT INTO portal_docs(claim_no,file_name,doc_type,sha256,idem_key,uploaded_at) VALUES(?,?,?,?,?,?)",
        ("CLM-1001", p.file_name, "final_invoice", p.sha256, "x", "now"))
    v = s.verify_upload("CLM-1001", p.file_name)
    assert v.state == VerificationState.FAILED and v.copies_found == 2 and "DUPLICATE" in v.reason
    s.portal.db.execute("DELETE FROM portal_docs WHERE claim_no='CLM-1001' AND sha256=?", (p.sha256,))
    v = s.verify_upload("CLM-1001", p.file_name)
    assert v.state == VerificationState.FAILED and v.copies_found == 0
    assert s.verify_upload("CLM-1001", "never_proposed.pdf").state == VerificationState.FAILED


def test_verifier_is_pending_when_portal_unreachable():
    s = svc()
    p = propose_tc1(s)
    s.execute_upload(p.proposal_id, True)
    s.portal.db.execute("DELETE FROM portal_claims WHERE claim_no='CLM-1001'")
    assert s.verify_upload("CLM-1001", p.file_name).state == VerificationState.PENDING
