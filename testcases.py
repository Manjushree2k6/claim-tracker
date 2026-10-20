"""testcases.py - the four official test cases (TC1-TC4), one call each.

Every run uses an isolated in-memory service so the demo database is not
disturbed. The human approval click is played by the harness and is labelled as
such in the explanation (the UI has the real Approve button).
"""
from __future__ import annotations

import json
import os
from typing import Any

import engine
import llm as llm_mod
from contract import (ExecutionState, QueryState, Status, TestCaseResult, VerificationState,
                      utc_now)
from service import ClaimService, NotAuthorized

EXPECTED = {
    "TC1": "Approved file is uploaded to the right claim (after human approval) and independently verified.",
    "TC2": "Another family member's claim needs that person's consent: no portal action before consent.",
    "TC3": "'Under review' is never reported as approved.",
    "TC4": "Upload times out: the document list is checked before any re-upload; exactly one copy ends up on the claim.",
}


class ScriptedLLM:
    """Deterministic stand-in for an LLM (used to test guards without network)."""

    def __init__(self, *texts: str) -> None:
        self.texts, self.calls = list(texts), 0

    def chat(self, system, messages, tools=None) -> dict:
        txt = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return {"content": [{"type": "text", "text": txt}]}


def _chk(checks: list[dict], name: str, ok: bool, detail: str = "") -> bool:
    checks.append({"name": name, "passed": bool(ok), "detail": detail})
    return bool(ok)


def _dump(x: Any) -> Any:
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


def _finish(tc: str, inp: dict, actual: str, checks: list[dict], evidence: list, expl: str,
            trace, svc: ClaimService) -> TestCaseResult:
    passed = all(c["passed"] for c in checks)
    return TestCaseResult(
        tc_id=tc, input=inp, expected=EXPECTED[tc], actual=actual, evidence=evidence, passed=passed,
        explanation=expl if passed else "FAILED CHECKS: " + "; ".join(
            c["name"] for c in checks if not c["passed"]) + ". " + expl,
        checks=checks, trace=[t for t in trace], portal_log=svc.portal.action_log())  # type: ignore[attr-defined]


def run(tc_id: str) -> TestCaseResult:
    fn = {"TC1": _tc1, "TC2": _tc2, "TC3": _tc3, "TC4": _tc4}.get(tc_id.upper())
    if not fn:
        raise ValueError(f"unknown test case {tc_id}")
    return fn()


# ---------------------------------------------------------------------- TC1
def _tc1() -> TestCaseResult:
    svc, checks = ClaimService.fresh(), []
    inp = {"question": "Where is my father's claim?", "claim_no": "CLM-1001", "policy_no": "POL-5001",
           "requester": "Priya Kumar (daughter, consent on file)", "consent_checkbox": True,
           "local_files": [f["file_name"] for f in svc.available_files()]}
    out = llm_mod.run_agent(svc, inp["question"], "CLM-1001", "POL-5001", "Priya Kumar", True, None)
    s = out.status
    _chk(checks, "state is OK", s.state == QueryState.OK, s.state.value)
    _chk(checks, "status DOCS_MISSING read from portal", s.status == Status.DOCS_MISSING, s.status.value)
    _chk(checks, "invoice listed as missing", s.missing_docs == ["final_invoice"], str(s.missing_docs))
    prop = out.proposals[0] if len(out.proposals) == 1 else None
    _chk(checks, "exactly one proposal; right file; right claim",
         bool(prop) and prop.file_name == "final_invoice_CLM-1001.pdf" and prop.destination_claim == "CLM-1001",
         f"{prop.file_name} -> {prop.destination_claim}" if prop else "no proposal")
    _chk(checks, "decoy file for another claim was not proposed",
         bool(prop) and "2002" not in prop.file_name)
    _chk(checks, "NO upload happened before approval", len(svc.portal.upload_attempts("CLM-1001")) == 0)  # type: ignore[attr-defined]
    ex = svc.execute_upload(prop.proposal_id, True) if prop else None
    _chk(checks, "execution EXECUTED", bool(ex) and ex.state == ExecutionState.EXECUTED, ex.state.value if ex else "")
    appr = svc.db.approvals_for(prop.proposal_id) if prop else []
    _chk(checks, "approval record stored with file hash + destination claim",
         len(appr) == 1 and appr[0]["decision"] == "APPROVED" and appr[0]["file_sha256"] == prop.sha256
         and appr[0]["destination_claim"] == "CLM-1001")
    ver = svc.verify_upload("CLM-1001", prop.file_name) if prop else None
    _chk(checks, "separate verifier: VERIFIED", bool(ver) and ver.state == VerificationState.VERIFIED,
         ver.reason if ver else "")
    _chk(checks, "file now in document list exactly once", bool(ver) and ver.copies_found == 1)
    other = svc.portal.list_documents("CLM-2002")["documents"]  # type: ignore[attr-defined]
    _chk(checks, "other claim untouched", len(other) == 2, f"CLM-2002 has {len(other)} documents")
    evidence = [{"status_evidence": [e.model_dump(mode="json") for e in s.evidence]},
                {"proposal": _dump(prop)}, {"approval_records": appr},
                {"execution": _dump(ex)}, {"verification": _dump(ver)}]
    actual = (f"{s.status.value}; missing={s.missing_docs}; upload={ex.state.value if ex else None}; "
              f"verifier={ver.state.value if ver else None}; docs before={ver.docs_before if ver else []} "
              f"-> after={ver.docs_after if ver else []}")
    return _finish("TC1", inp, actual, checks, evidence,
                   "The human 'Approve' click is played by this test harness; in the app it is a real button. "
                   "The portal is our synthetic mock, not the organiser's portal.", out.trace, svc)


# ---------------------------------------------------------------------- TC2
def _tc2() -> TestCaseResult:
    svc, checks = ClaimService.fresh(), []
    inp = {"question": "Where is my aunt's claim?", "claim_no": "CLM-2002", "policy_no": "POL-5002",
           "requester": "Anil Rao (relative, NOT the holder)", "consent_checkbox": True}
    out = llm_mod.run_agent(svc, inp["question"], "CLM-2002", "POL-5002", "Anil Rao", True, None)
    s = out.status
    _chk(checks, "state CONSENT_REQUIRED", s.state == QueryState.CONSENT_REQUIRED, s.state.value)
    _chk(checks, "ticking the checkbox alone did not grant access", s.rule_fired == "R3-no-consent", s.rule_fired)
    _chk(checks, "ZERO portal actions logged", len(svc.portal.action_log()) == 0,  # type: ignore[attr-defined]
         f"{len(svc.portal.action_log())} portal actions")  # type: ignore[attr-defined]
    blocked = False
    try:
        svc.propose_upload("CLM-2002", "final_invoice_CLM-2002.pdf")
    except NotAuthorized:
        blocked = True
    _chk(checks, "upload proposal blocked without consent", blocked)
    _chk(checks, "still zero portal actions after the blocked attempt",
         len(svc.portal.action_log()) == 0)  # type: ignore[attr-defined]
    svc.record_holder_consent("CLM-2002", "Anil Rao")  # SIMULATED holder action
    after = svc.get_claim_status("CLM-2002", "POL-5002", "Anil Rao", True)
    _chk(checks, "after the holder's (simulated) consent the claim can be read", after.state == QueryState.OK,
         after.state.value)
    actual = (f"Before consent: {s.state.value} (rule {s.rule_fired}), 0 portal actions. "
              f"After recorded consent: {after.state.value} / {after.status.value}.")
    ev = [{"blocked_result": _dump(s)}, {"portal_actions_before_consent": 0},
          {"after_consent_status": after.status.value}]
    return _finish("TC2", inp, actual, checks, ev,
                   "The holder's consent is simulated by a button/function in this demo; no real consent "
                   "request is sent to anyone.", out.trace + after.trace, svc)


# ---------------------------------------------------------------------- TC3
def _tc3() -> TestCaseResult:
    svc, checks = ClaimService.fresh(), []
    inp = {"question": "Has my mother's claim been approved yet?", "claim_no": "CLM-3003",
           "policy_no": "POL-5003", "requester": "Rohan Nair (son, consent on file)", "consent_checkbox": True}
    out = llm_mod.run_agent(svc, inp["question"], "CLM-3003", "POL-5003", "Rohan Nair", True, None)
    s = out.status
    shown = " ".join([out.message, s.next_step, s.reason, s.status.value.replace("_", " ")])
    _chk(checks, "portal raw status read", s.raw_status == "Under review", str(s.raw_status))
    _chk(checks, "mapped to UNDER_REVIEW", s.status == Status.UNDER_REVIEW, s.status.value)
    _chk(checks, "state OK", s.state == QueryState.OK)
    _chk(checks, "no approval wording in message / next step / reason",
         not engine._APPROVAL_WORDS.search(" ".join([out.message, s.next_step, s.reason])), out.message)
    bad = llm_mod.phrase_result(s, ScriptedLLM("Good news! Your claim has been approved."))
    _chk(checks, "a rogue LLM message saying 'approved' is rejected and replaced by the template",
         bad[1] is False and not engine._APPROVAL_WORDS.search(bad[0]), bad[2])
    ok_msg = llm_mod.phrase_result(s, ScriptedLLM("Your claim is still being reviewed by the insurer."))
    _chk(checks, "a harmless LLM message is accepted", ok_msg[1] is True, ok_msg[2])
    _chk(checks, "'under review' text itself is not treated as approval",
         engine.map_status("Under review") != Status.APPROVED and engine.map_status("not approved") == Status.UNKNOWN)
    actual = f"raw='{s.raw_status}' -> {s.status.value}. Message shown: \"{out.message}\" Next step: \"{s.next_step}\""
    ev = [{"status_evidence": [e.model_dump(mode="json") for e in s.evidence]},
          {"message_shown": out.message}, {"rogue_llm_guard": {"candidate": "Good news! Your claim has been approved.",
                                                               "used": bad[0], "note": bad[2]}}]
    return _finish("TC3", inp, actual, checks, ev,
                   "The rogue-LLM check uses a scripted fake model, not a live API call.", out.trace, svc)


# ---------------------------------------------------------------------- TC4
def _tc4() -> TestCaseResult:
    svc, checks = ClaimService.fresh(), []
    inp = {"claims": ["CLM-4004 (portal times out AFTER storing the file)",
                      "CLM-4005 (portal times out BEFORE storing the file)"],
           "requesters": ["Divya Iyer", "Karthik Menon"], "consent_checkbox": True}
    results = {}
    traces = []
    for claim, pol, who in (("CLM-4004", "POL-5004", "Divya Iyer"), ("CLM-4005", "POL-5005", "Karthik Menon")):
        out = llm_mod.run_agent(svc, "What is missing?", claim, pol, who, True, None)
        traces += out.trace
        prop = out.proposals[0] if out.proposals else None
        ex = svc.execute_upload(prop.proposal_id, True) if prop else None
        ver = svc.verify_upload(claim, prop.file_name) if prop else None
        attempts = svc.portal.upload_attempts(claim)  # type: ignore[attr-defined]
        results[claim] = (prop, ex, ver, attempts)
    p_a, e_a, v_a, up_a = results["CLM-4004"]
    p_b, e_b, v_b, up_b = results["CLM-4005"]
    _chk(checks, "A: timeout detected, list re-read, file found -> SKIPPED_DUPLICATE",
         bool(e_a) and e_a.state == ExecutionState.SKIPPED_DUPLICATE, e_a.state.value if e_a else "")
    _chk(checks, "A: portal received exactly ONE upload call (no re-upload)", len(up_a) == 1, f"{len(up_a)} calls")
    _chk(checks, "A: log shows the list was re-read before any retry",
         bool(e_a) and any("re-reading document list" in l for l in e_a.log))
    _chk(checks, "A: verifier VERIFIED, exactly one copy",
         bool(v_a) and v_a.state == VerificationState.VERIFIED and v_a.copies_found == 1,
         v_a.reason if v_a else "")
    _chk(checks, "B: list re-read, file absent -> retried once with the same key -> EXECUTED",
         bool(e_b) and e_b.state == ExecutionState.EXECUTED and e_b.attempts == 2,
         f"{e_b.state.value}, attempts={e_b.attempts}" if e_b else "")
    _chk(checks, "B: both portal calls used the same idempotency key",
         len(up_b) == 2 and all(p_b.idempotency_key in r["detail"] for r in up_b) if p_b else False)
    _chk(checks, "B: verifier VERIFIED, exactly one copy",
         bool(v_b) and v_b.state == VerificationState.VERIFIED and v_b.copies_found == 1)
    actual = (f"A (timeout after commit): {e_a.state.value}, upload calls={len(up_a)}, verifier={v_a.state.value}, "
              f"copies={v_a.copies_found}. B (timeout before commit): {e_b.state.value}, "
              f"attempts={e_b.attempts}, verifier={v_b.state.value}, copies={v_b.copies_found}.")
    ev = [{"A_execution": _dump(e_a), "A_verification": _dump(v_a), "A_portal_upload_log": up_a},
          {"B_execution": _dump(e_b), "B_verification": _dump(v_b), "B_portal_upload_log": up_b}]
    return _finish("TC4", inp, actual, checks, ev,
                   "Timeouts are INJECTED by the mock portal (fault mode). How the organiser's portal "
                   "behaves on timeout is still to be confirmed with the organiser.", traces, svc)


def export_evidence(res: TestCaseResult, folder: str = "evidence") -> str:
    """Write one test run as pretty JSON (committed samples live in /evidence)."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{res.tc_id}_evidence.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"exported_at": utc_now(), **res.model_dump(mode="json")}, fh, indent=2)
    return path
