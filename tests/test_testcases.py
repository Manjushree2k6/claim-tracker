"""One-click official test cases must all pass (and export valid evidence)."""
import json
import os
import tempfile

from helpers import svc

import testcases


def _run(tc):
    r = testcases.run(tc)
    assert r.passed, [c for c in r.checks if not c["passed"]]
    assert r.input and r.expected and r.actual and r.evidence and r.explanation
    return r


def test_tc1_missing_invoice():
    _run("TC1")


def test_tc2_other_family_member_needs_consent():
    r = _run("TC2")
    assert r.portal_log and r.portal_log[0]["claim_no"] == "CLM-2002"  # only AFTER consent was recorded


def test_tc3_under_review_never_approved():
    _run("TC3")


def test_tc4_upload_timeout():
    _run("TC4")


def test_run_test_case_saves_result_and_exports_evidence():
    s = svc()
    r = s.run_test_case("TC3")
    assert s.db.latest_test_results()["TC3"]["passed"] == 1
    path = testcases.export_evidence(r, tempfile.mkdtemp())
    data = json.load(open(path, encoding="utf-8"))
    assert data["tc_id"] == "TC3" and data["passed"] is True and os.path.getsize(path) > 500
