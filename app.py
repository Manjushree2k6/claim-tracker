"""app.py - Streamlit UI for the Insurance Claim Status Tracker agent.

Pages: Ask the agent | Test cases | Audit log | Safety & limitations.
Run state lives in SQLite (proposals/approvals/executions) plus st.session_state,
so an approval screen survives Streamlit reruns.
"""
from __future__ import annotations

import html
import json

import streamlit as st

import engine
import llm as llm_mod
import report as report_mod
import testcases
from contract import (ExecutionState, QueryState, Status, VerificationState)
from service import ClaimService

st.set_page_config(page_title="Claim Status Tracker", page_icon="🩺", layout="wide")

COLORS = {  # status -> (background, text)
    "APPROVED": ("#d1fae5", "#065f46"), "UNDER_REVIEW": ("#fef3c7", "#92400e"),
    "REJECTED": ("#fee2e2", "#991b1b"), "DOCS_MISSING": ("#dbeafe", "#1e40af"),
    "UNKNOWN": ("#e5e7eb", "#374151"), "CONSENT_REQUIRED": ("#fde68a", "#78350f"),
    "UNCERTAIN": ("#e5e7eb", "#374151"), "EXECUTED": ("#d1fae5", "#065f46"),
    "SKIPPED_DUPLICATE": ("#dbeafe", "#1e40af"), "TIMEOUT": ("#fee2e2", "#991b1b"),
    "FAILED": ("#fee2e2", "#991b1b"), "VERIFIED": ("#d1fae5", "#065f46"),
    "PENDING": ("#fef3c7", "#92400e"), "PASS": ("#d1fae5", "#065f46"),
}
PLAIN = {
    "UNDER_REVIEW": "UNDER REVIEW", "DOCS_MISSING": "DOCUMENTS MISSING", "CONSENT_REQUIRED": "CONSENT NEEDED",
    "UNCERTAIN": "NOT SURE - CHECK MANUALLY", "UNKNOWN": "UNKNOWN", "SKIPPED_DUPLICATE": "ALREADY THERE - NOT RE-UPLOADED",
    "EXECUTED": "UPLOADED", "TIMEOUT": "TIMED OUT", "FAILED": "FAILED", "VERIFIED": "VERIFIED", "PENDING": "PENDING",
}


def badge(code: str, text: str | None = None) -> None:
    bg, fg = COLORS.get(code, ("#e5e7eb", "#374151"))
    label = html.escape(text or PLAIN.get(code, code))
    st.markdown(f"<span style='background:{bg};color:{fg};padding:4px 12px;border-radius:14px;"
                f"font-weight:700;font-size:0.95rem'>{label}</span>", unsafe_allow_html=True)


@st.cache_resource
def get_service() -> ClaimService:
    return ClaimService()


@st.cache_resource
def get_llm():
    return llm_mod.default_client()


svc = get_service()


def short(x, n: int = 150) -> str:
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def trace_table(trace) -> None:
    rows = [{"time": t.timestamp[11:19], "step": t.step, "tool": t.tool, "input": short(t.input),
             "output": short(t.output)} for t in trace]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def evidence_table(evidence) -> None:
    st.dataframe([{"field": e.field, "value": short(e.value), "source": e.source_id_or_url,
                   "retrieved_at": e.retrieved_at} for e in evidence], use_container_width=True, hide_index=True)


# ============================================================ ASK PAGE
def load_scenario(sc: dict) -> None:
    st.session_state.update(f_question=sc["question"], f_claim=sc["claim_no"], f_policy=sc["policy_no"],
                            f_requester=sc["requester"], f_consent=sc["consent"])
    st.session_state.pop("outcome", None)


def run_question() -> None:
    s = st.session_state
    use_ai = s.get("use_ai", False) and get_llm() is not None
    s["last_inputs"] = dict(question=s.f_question, claim_no=s.f_claim, policy_no=s.f_policy,
                            requester=s.f_requester, consent=bool(s.f_consent), use_ai=use_ai)
    s["outcome"] = llm_mod.run_agent(svc, s.f_question, s.f_claim, s.f_policy, s.f_requester,
                                     bool(s.f_consent), get_llm() if use_ai else None)


def simulate_consent(claim_no: str, requester: str) -> None:
    """Callback (runs BEFORE widgets are rebuilt, so setting f_consent is allowed)."""
    svc.record_holder_consent(claim_no, requester)
    st.session_state.f_consent = True
    run_question()


def page_ask() -> None:
    st.title("🩺 Where is my family member's claim?")
    st.caption("Ask in plain words. The agent reads the insurer portal, tells you the real status, lists what is "
               "missing, and - only with your approval - uploads the right document. Demo uses synthetic data only.")
    scenarios = svc.registry.demo_scenarios
    for k, v in dict(f_question="Where is my father's claim?", f_claim="", f_policy="", f_requester="",
                     f_consent=False).items():
        st.session_state.setdefault(k, v)
    c1, c2 = st.columns([3, 1])
    pick = c1.selectbox("Quick demo scenarios (optional)", range(len(scenarios)),
                        format_func=lambda i: scenarios[i]["label"], index=None, placeholder="Choose a scenario...")
    if pick is not None:
        c2.button("Fill the form", on_click=load_scenario, args=(scenarios[pick],), use_container_width=True)

    with st.form("ask"):
        st.text_area("Your question", key="f_question", height=70)
        a, b = st.columns(2)
        a.text_input("Claim number", key="f_claim", placeholder="CLM-1001")
        b.text_input("Policy number", key="f_policy", placeholder="POL-5001")
        st.text_input("Your full name (the person asking)", key="f_requester", placeholder="Priya Kumar")
        st.checkbox("I confirm the policy holder has agreed that I can see this claim", key="f_consent")
        go = st.form_submit_button("Check claim status", type="primary", use_container_width=True)
    if go:
        run_question()
    out = st.session_state.get("outcome")
    if out:
        render_outcome(out)


def render_outcome(out) -> None:
    s = out.status
    st.divider()
    if s.state == QueryState.CONSENT_REQUIRED:
        badge("CONSENT_REQUIRED")
        st.warning(out.message)
        st.markdown(f"**Next step:** {s.next_step}")
        st.caption("No request was sent to the insurer portal. A family relationship alone does not give access.")
        inp = st.session_state.get("last_inputs", {})
        with st.expander("Demo only: simulate the policy holder giving consent"):
            st.caption("In real life the policy holder would approve this themselves. This button only records a "
                       "mock consent so you can see the next step.")
            st.button("Simulate: holder gives consent to this requester", on_click=simulate_consent,
                      args=(s.claim_no, inp.get("requester", "")))
    elif s.state == QueryState.UNCERTAIN:
        badge("UNCERTAIN")
        st.info(out.message)
        st.markdown(f"**What we found:** {s.reason}")
        st.markdown(f"**Next step:** {s.next_step}")
    else:
        badge(s.status.value)
        st.markdown(f"#### {out.message}")
        left, right = st.columns(2)
        with left:
            st.markdown("**Missing items**")
            if s.missing_docs:
                cands = engine.candidate_files(s.claim_no, s.missing_docs, svc.available_files())
                for m in s.missing_docs:
                    n = len(cands.get(m, []))
                    st.markdown(f"- {engine.label(m)} - " + ("file found on your device" if n == 1 else
                                f"{n} possible files, please choose" if n > 1 else "no matching file yet"))
            else:
                st.markdown("- Nothing missing.")
        with right:
            st.markdown("**Next step**")
            st.write(s.next_step)
        st.caption(f"Source: insurer portal status '{s.raw_status}' (stamp {s.status_stamp}). Rule: {s.rule_fired}.")
    if out.llm_note:
        st.caption(f"Message written by: {'AI (validated by code)' if out.used_llm else out.llm_note}")
    rep_json, rep_text = report_mod.build_report(s, out.message, st.session_state.get("last_inputs", {}))
    fname = f"claim_status_{s.claim_no or 'unknown'}"
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("Download status report", rep_text, file_name=f"{fname}.txt", mime="text/plain",
                       key=f"dl_txt_{s.run_id}", help="Readable report: status, missing items, next step, evidence.")
    d2.download_button("Download as JSON", json.dumps(rep_json, indent=2, default=str), file_name=f"{fname}.json",
                       mime="application/json", key=f"dl_json_{s.run_id}")
    if s.evidence:
        with st.expander("Evidence (where each fact came from)"):
            evidence_table(s.evidence)
    with st.expander("Agent trace (every step, with timestamps)", expanded=False):
        trace_table(out.trace)
    for p in out.proposals:
        render_proposal(p)


# ---------------------------------------------------------- approval step
def render_proposal(p) -> None:
    st.divider()
    st.subheader("Approval needed before anything is uploaded")
    row = svc.db.get_proposal(p.proposal_id)
    ex_row = svc.db.latest_execution(p.proposal_id)
    decided = row and row["status"] in ("DONE", "REJECTED")
    if not decided:
        st.markdown(f"**File:** `{p.file_name}`  \n**Destination claim:** `{p.destination_claim}`  \n"
                    f"**Exact change:** {p.change_summary}")
        st.caption(f"Idempotency key: {p.idempotency_key[:40]}... (prevents a second copy if the portal times out)")
        a, b = st.columns(2)
        if a.button("✅ Approve and upload", key=f"ok_{p.proposal_id}", type="primary", use_container_width=True):
            ex = svc.execute_upload(p.proposal_id, True)
            ver = svc.verify_upload(p.destination_claim, p.file_name)  # separate module
            st.session_state.setdefault("results", {})[p.proposal_id] = (ex, ver)
            st.rerun()
        if b.button("❌ Reject", key=f"no_{p.proposal_id}", use_container_width=True):
            svc.execute_upload(p.proposal_id, False)
            st.rerun()
    res = st.session_state.get("results", {}).get(p.proposal_id)
    if row and row["status"] == "REJECTED":
        st.info("You rejected this upload. Nothing was sent to the insurer.")
    if res or (ex_row and row and row["status"] == "DONE"):
        ex, ver = res if res else (None, None)
        st.markdown("**Result of the upload**")
        state = ex.state.value if ex else ex_row["state"]
        badge(state)
        st.write(ex.reason if ex else ex_row["reason"])
        log = ex.log if ex else json.loads(ex_row["log_json"] or "[]")
        if log:
            with st.expander("Upload log"):
                for line in log:
                    st.write("• " + line)
        if st.button("Re-check with the portal (independent verification)", key=f"v_{p.proposal_id}") or ver is None:
            ver = svc.verify_upload(p.destination_claim, p.file_name)
        st.markdown("**Independent verification** (re-reads the document list and status stamp)")
        badge(ver.state.value)
        st.write(ver.reason)
        c1, c2 = st.columns(2)
        c1.markdown("Documents before"); c1.write(ver.docs_before)
        c2.markdown("Documents after"); c2.write(ver.docs_after)
        st.caption(f"Status stamp now: {ver.status_stamp}")
        if ver.state != VerificationState.VERIFIED:
            st.error("Not verified. This is shown as it is - please check with the insurer.")


# ============================================================ TEST PAGE
def page_tests() -> None:
    st.title("Official test cases")
    st.caption("One click per case. Each run uses an isolated copy of the synthetic data and saves its evidence. "
               "The human 'Approve' click is performed by the test harness and is stated in each explanation.")
    titles = {"TC1": "TC1 - Missing invoice requested", "TC2": "TC2 - Another family member's claim",
              "TC3": "TC3 - 'Under review' never shown as approved", "TC4": "TC4 - Upload times out"}
    if st.button("Run all four", type="primary"):
        for tc in titles:
            run_and_store(tc)
    saved = svc.db.latest_test_results()
    for tc, title in titles.items():
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            c1.subheader(title)
            if c2.button("Run", key=f"run_{tc}", use_container_width=True):
                run_and_store(tc)
                saved = svc.db.latest_test_results()
            row = saved.get(tc)
            if not row:
                st.caption("Not run yet.")
                continue
            data = json.loads(row["result_json"])
            badge("PASS" if data["passed"] else "FAILED", "PASSED" if data["passed"] else "FAILED")
            st.caption(f"Last run: {row['ts']}")
            st.markdown(f"**Expected:** {data['expected']}")
            st.markdown(f"**Actual:** {data['actual']}")
            st.markdown(f"**Explanation:** {data['explanation']}")
            st.dataframe([{"check": c["name"], "result": "pass" if c["passed"] else "FAIL", "detail": c["detail"]}
                          for c in data["checks"]], use_container_width=True, hide_index=True)
            with st.expander("Input"):
                st.json(data["input"])
            with st.expander("Evidence"):
                st.json(data["evidence"])
            with st.expander("Portal action log"):
                st.dataframe(data["portal_log"], use_container_width=True, hide_index=True)
            st.download_button("Download evidence (JSON)", json.dumps(data, indent=2, default=str),
                               file_name=f"{tc}_evidence.json", mime="application/json", key=f"dl_{tc}")


def run_and_store(tc: str) -> None:
    res = svc.run_test_case(tc)
    try:
        testcases.export_evidence(res, "evidence")  # may be read-only/ephemeral on free hosting
    except OSError:
        pass


# ============================================================ AUDIT PAGE
def page_audit() -> None:
    st.title("Audit log")
    st.caption("Every decision is stored: input, source, retrieval time, rule fired, reason.")
    runs = svc.db.query("SELECT id,ts,claim_no,requester,consent_flag,state,status,rule_fired,reason "
                        "FROM runs ORDER BY id DESC LIMIT 100")
    appr = svc.db.query("SELECT * FROM approvals ORDER BY id DESC LIMIT 100")
    execs = svc.db.query("SELECT id,ts,proposal_id,claim_no,idem_key,state,attempts,reason,log_json "
                         "FROM executions ORDER BY id DESC LIMIT 100")
    vers = svc.db.query("SELECT id,ts,claim_no,file_name,state FROM verifications ORDER BY id DESC LIMIT 100")
    plog = svc.db.query("SELECT * FROM portal_log ORDER BY id DESC LIMIT 200")
    t = st.tabs(["Status checks", "Approvals", "Uploads", "Verifications", "Portal actions"])
    for tab, rows in zip(t, (runs, appr, execs, vers, plog)):
        with tab:
            if rows:
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.caption("Nothing yet.")
    st.download_button("Download full audit (JSON)", json.dumps(
        {"status_checks": runs, "approvals": appr, "uploads": execs, "verifications": vers, "portal_actions": plog},
        indent=2, default=str), file_name="audit_log.json", mime="application/json")


# ============================================================ ABOUT PAGE
def page_about() -> None:
    st.title("Safety rules, evidence map and honest limitations")
    st.markdown("""
**How it works:** `PLAN → TOOL CALL → OBSERVE → DECIDE → APPROVE → ACT → VERIFY`.
Code decides status, consent, missing documents, duplicates and verification. An AI model (optional) may only
parse the question, pick tools, suggest a file and word the answer - and every AI output is checked by code.
""")
    st.markdown("#### Safety rules enforced in code")
    st.markdown("""
1. Status comes from the portal; unrecognised status → *Unknown / check manually*. 'Under review' is never shown as approved.
2. Not the policy holder and no recorded consent → **stop before any portal action** (a family tie alone is not consent).
3. No upload without a stored approval record that shows file, destination claim and exact change.
4. Idempotency key = claim + file fingerprint. On a timeout the document list is re-read first; retry at most once.
5. Verification is a separate module that re-reads the document list and status stamp.
6. Failed and pending results stay visible. Synthetic data only. OTP/CAPTCHA are never bypassed.
""")
    st.markdown("#### Evidence map")
    st.dataframe([
        {"Requirement": "Reads actual status", "Proof": "Portal adapter + status mapping", "Where": "TC1, TC3 evidence"},
        {"Requirement": "Requires consent", "Proof": "Consent check before portal", "Where": "TC2: zero portal actions"},
        {"Requirement": "Approval before upload", "Proof": "Stored approval with file hash + destination", "Where": "TC1 approval record"},
        {"Requirement": "No duplicate upload", "Proof": "Idempotency key + list re-read", "Where": "TC4 upload log"},
        {"Requirement": "Verifies upload", "Proof": "Separate verifier, before/after lists, stamp", "Where": "TC1, TC4 verification"},
        {"Requirement": "Never 'approved' when under review", "Proof": "Exact status map + output validator", "Where": "TC3"},
        {"Requirement": "Works when AI fails", "Proof": "Template fallback", "Where": "tests/test_llm.py"},
    ], use_container_width=True, hide_index=True)
    st.markdown("#### Honest limitations")
    st.markdown("""
- Runs against **our synthetic mock portal**. The organiser's portal is not connected yet; its fields, timeout
  behaviour and access method are assumptions flagged in `portal.py`.
- Consent is simulated with a mock button; no real message is sent to a policy holder.
- Identity is not verified: anyone who types the policy holder's exact name is treated as the holder. A real system
  would use the insurer's login/OTP, which we deliberately do not simulate or bypass.
- Timeouts are *injected* by the mock portal.
- Free hosting storage can reset on restart; evidence is downloadable and sample files are committed in `/evidence`.
- The optional AI path is covered by automated tests with scripted model responses (guards, tool restrictions, fallback). Live model output varies, so every AI message is validated by code and each result shows whether AI or the template wrote it.
""")


# ============================================================ MAIN
def main() -> None:
    with st.sidebar:
        st.header("Claim Status Tracker")
        page = st.radio("Page", ["Ask the agent", "Test cases", "Audit log", "Safety & limitations"], label_visibility="collapsed")
        has_ai = get_llm() is not None
        st.toggle("Use AI assistant", key="use_ai", value=has_ai, disabled=not has_ai)
        if has_ai and st.session_state.get("use_ai"):
            st.caption(f"AI on: {getattr(get_llm(), 'label', 'AI model')} (every output validated by code).")
        else:
            st.caption("Template mode: fully working without any AI key.")
        st.divider()
        st.caption("🔒 Synthetic data only. Nothing here is real.")
        if st.checkbox("Show reset option"):
            if st.button("Reset demo data"):
                svc.reset_demo()
                for k in ("outcome", "results", "last_inputs"):
                    st.session_state.pop(k, None)
                st.rerun()
    {"Ask the agent": page_ask, "Test cases": page_tests, "Audit log": page_audit,
     "Safety & limitations": page_about}[page]()


main()