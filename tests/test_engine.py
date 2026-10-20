"""Unit tests for engine.py (one or more per engine function)."""
from helpers import svc  # noqa: F401  (sets sys.path)

import engine
from contract import QueryState, Status


def test_map_status_known_values():
    assert engine.map_status("Under review") == Status.UNDER_REVIEW
    assert engine.map_status("  UNDER   REVIEW! ") == Status.UNDER_REVIEW
    assert engine.map_status("Approved") == Status.APPROVED
    assert engine.map_status("Rejected") == Status.REJECTED
    assert engine.map_status("Documents Pending") == Status.DOCS_MISSING


def test_map_status_unmapped_is_unknown_never_approved():
    for raw in (None, "", "Pending Reconciliation", "not approved", "approval pending",
                "Disapproved", "approved?", "Under review - approved soon"):
        assert engine.map_status(raw) == Status.UNKNOWN, raw


def test_validate_inputs():
    assert engine.validate_inputs("CLM-1001", "POL-5001", "Priya Kumar") == []
    assert len(engine.validate_inputs("1001", "5001", "")) == 3
    assert engine.validate_inputs("CLM-1001", "POL-5001", "Robert'); DROP TABLE--")  # rejected


def test_compute_missing():
    assert engine.compute_missing(["a", "b", "c"], {"a", "c"}) == ["b"]
    assert engine.compute_missing(["a"], {"a"}) == []
    assert engine.compute_missing(["a", "b"], set()) == ["a", "b"]


def test_file_mapping_right_claim_only():
    assert engine.file_matches("final_invoice", "final_invoice_CLM-1001.pdf", "CLM-1001")
    assert not engine.file_matches("final_invoice", "final_invoice_CLM-2002.pdf", "CLM-1001")
    assert not engine.file_matches("final_invoice", "holiday_photo.pdf", "CLM-1001")
    assert engine.file_matches("final_invoice", "my_hospital_bill.pdf", "CLM-1001")  # untagged ok


def test_candidate_files_ambiguity_is_visible():
    files = [{"file_name": "invoice_a.pdf", "sha256": "1"}, {"file_name": "invoice_b.pdf", "sha256": "2"}]
    c = engine.candidate_files("CLM-1001", ["final_invoice", "id_proof"], files)
    assert len(c["final_invoice"]) == 2 and c["id_proof"] == []
    assert "provide" in engine.next_step(Status.DOCS_MISSING, ["final_invoice"], c, None).lower()


def test_idempotency_key():
    assert engine.idempotency_key("CLM-1", "abc") == "CLM-1:abc"
    assert engine.idempotency_key("CLM-1", "abc") != engine.idempotency_key("CLM-2", "abc")


def test_consent_rules():
    s = svc()
    reg = s.registry
    a = reg.check_authorization(s.db, "CLM-1001", "POL-5001", "Ramesh Kumar", False)
    assert a.authorized and a.rule == "R3-requester-is-holder"
    a = reg.check_authorization(s.db, "CLM-1001", "POL-5001", "ramesh   kumar", False)
    assert a.authorized  # name normalisation
    a = reg.check_authorization(s.db, "CLM-1001", "POL-5001", "Priya Kumar", True)
    assert a.authorized and a.rule == "R3-recorded-consent"
    a = reg.check_authorization(s.db, "CLM-1001", "POL-5001", "Priya Kumar", False)
    assert not a.authorized and a.state == QueryState.CONSENT_REQUIRED  # recorded but not confirmed
    a = reg.check_authorization(s.db, "CLM-2002", "POL-5002", "Anil Rao", True)
    assert not a.authorized and a.state == QueryState.CONSENT_REQUIRED  # checkbox alone is not consent
    a = reg.check_authorization(s.db, "CLM-1001", "POL-5002", "Ramesh Kumar", True)
    assert a.state == QueryState.UNCERTAIN  # claim/policy mismatch


def test_validate_message_blocks_approval_wording_and_invented_facts():
    facts = '{"claim": "CLM-3003", "status": "UNDER_REVIEW"}'
    ok, _ = engine.validate_message("Your claim is under review.", Status.UNDER_REVIEW, facts)
    assert ok
    for bad in ("Good news, it is approved!", "Your claim was settled.", "It has been sanctioned.",
                "Claim CLM-9999 is under review.", "Expect a decision in 7 days.", "See invoice_x.pdf",
                "Decision in 3 days.", "Claim CLM-30 is under review."):  # '3' / 'CLM-30' hide inside 'CLM-3003'
        ok, why = engine.validate_message(bad, Status.UNDER_REVIEW, facts)
        assert not ok, (bad, why)
    ok, _ = engine.validate_message("The portal shows this claim as approved.", Status.APPROVED, facts)
    assert ok  # allowed only when the PORTAL status is APPROVED


def test_template_messages_never_say_approved_unless_approved():
    for st in (Status.UNDER_REVIEW, Status.DOCS_MISSING, Status.REJECTED, Status.UNKNOWN):
        for state in QueryState:
            msg = engine.template_message(st, state, ["final_invoice"], "reason text")
            assert not engine._APPROVAL_WORDS.search(msg), (st, state, msg)
        assert not engine._APPROVAL_WORDS.search(engine.next_step(st, ["final_invoice"], {}, None))
