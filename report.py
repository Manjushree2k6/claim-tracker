"""report.py - builds the downloadable claim status report (pure functions, no Streamlit).

The report is generated from the validated StatusResult, i.e. from what the portal said and
what code decided, never from model text. When consent is missing or the status is uncertain,
no claim details (documents, evidence) are included.
"""
from __future__ import annotations

import engine
from contract import QueryState, Status, StatusResult, utc_now

STATUS_TEXT = {
    Status.APPROVED: "Approved",
    Status.UNDER_REVIEW: "Under review",
    Status.REJECTED: "Rejected",
    Status.DOCS_MISSING: "Documents missing",
    Status.UNKNOWN: "Unknown - manual verification required",
}
STATE_TEXT = {
    QueryState.OK: "Status read from the insurer portal",
    QueryState.CONSENT_REQUIRED: "Consent required - no portal request was made",
    QueryState.UNCERTAIN: "Not sure - please check manually",
}
NOTICE = "Synthetic demo data from a mock insurer portal. Not a real claim."


def build_report(res: StatusResult, message: str = "", inputs: dict | None = None) -> tuple[dict, str]:
    """Returns (report_dict, readable_text). `inputs` is the validated form (claim, policy, requester)."""
    inputs = inputs or {}
    details_allowed = res.state == QueryState.OK
    data: dict = {
        "report": "Claim status report",
        "generated_at": utc_now(),
        "notice": NOTICE,
        "request": {
            "claim_no": res.claim_no or inputs.get("claim_no", ""),
            "policy_no": inputs.get("policy_no", ""),
            "requester": inputs.get("requester", ""),
        },
        "state": res.state.value,
        "state_text": STATE_TEXT.get(res.state, res.state.value),
        "status": res.status.value,
        "status_text": STATUS_TEXT.get(res.status, res.status.value),
        "next_step": res.next_step,
        "reason": res.reason,
        "rule_fired": res.rule_fired,
        "summary": message,
    }
    if details_allowed:
        data.update({
            "portal_status_text": res.raw_status,
            "status_stamp": res.status_stamp,
            "missing_documents": [engine.label(m) for m in res.missing_docs],
            "documents_on_file": list(res.present_docs),
            "evidence": [e.model_dump(mode="json") for e in res.evidence],
        })
    else:
        data["details_withheld"] = "Claim details are not included because access was not confirmed."

    lines = [
        "CLAIM STATUS REPORT",
        "=" * 19,
        NOTICE,
        f"Generated (UTC): {data['generated_at']}",
        "",
        f"Claim:        {data['request']['claim_no'] or '-'}",
        f"Policy:       {data['request']['policy_no'] or '-'}",
        f"Requested by: {data['request']['requester'] or '-'}",
        "",
        f"Result:       {data['state_text']}",
        f"Status:       {data['status_text']}",
    ]
    if details_allowed:
        lines.append(f"Portal text:  {res.raw_status}   (stamp {res.status_stamp})")
        lines += ["", "Missing documents:"]
        lines += [f"  - {x}" for x in data["missing_documents"]] or ["  - none"]
        lines += ["", "Documents on file:"]
        lines += [f"  - {x}" for x in data["documents_on_file"]] or ["  - none"]
    lines += ["", "Next step:", f"  {res.next_step or '-'}"]
    if res.reason:
        lines += ["", "Why:", f"  {res.reason}"]
    if message:
        lines += ["", "Summary:", f"  {message}"]
    if not details_allowed:
        lines += ["", data["details_withheld"]]
    if details_allowed and res.evidence:
        lines += ["", "Evidence (source, time, field, value):"]
        lines += [f"  - {e.source_id_or_url} | {e.retrieved_at} | {e.field} | {e.value}" for e in res.evidence]
    lines += ["", f"Decision rule: {res.rule_fired or '-'}"]
    return data, "\n".join(lines) + "\n"