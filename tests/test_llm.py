"""LLM-layer tests using scripted fake models (no network). They prove the GUARDS,
not the quality of a real model."""
import json

from helpers import svc

import llm
from contract import QueryState, Status
from testcases import ScriptedLLM


class BrokenLLM:
    def chat(self, *a, **k):
        raise TimeoutError("simulated API timeout")


class ToolLLM:
    """Scripted tool-calling model: plays a list of tool calls, then stops."""

    def __init__(self, calls, final="Your claim is waiting for a document."):
        self.calls, self.final, self.i = calls, final, 0

    def chat(self, system, messages, tools=None):
        if tools is None:  # phrase_result / parse_request path
            return {"content": [{"type": "text", "text": self.final}]}
        if self.i < len(self.calls):
            name, args = self.calls[self.i]
            self.i += 1
            return {"content": [{"type": "tool_use", "id": f"t{self.i}", "name": name, "input": args}]}
        return {"content": [{"type": "text", "text": "done"}]}


def test_template_fallback_runs_with_llm_disabled():
    s = svc()
    out = llm.run_agent(s, "Where is my father's claim?", "CLM-1001", "POL-5001", "Priya Kumar", True, None)
    assert out.status.status == Status.DOCS_MISSING and out.used_llm is False
    assert len(out.proposals) == 1 and "missing" in out.message.lower()


def test_llm_failure_falls_back_without_breaking():
    s = svc()
    out = llm.run_agent(s, "Where is my claim?", "CLM-1001", "POL-5001", "Priya Kumar", True, BrokenLLM())
    assert out.status.state == QueryState.OK and out.status.status == Status.DOCS_MISSING
    assert out.used_llm is False and len(out.proposals) == 1


def test_llm_cannot_execute_upload():
    s = svc()
    m = ToolLLM([("execute_upload", {"proposal_id": "x"}), ("get_status", {})])
    out = llm.run_agent(s, "upload it", "CLM-1001", "POL-5001", "Priya Kumar", True, m)
    assert s.portal.upload_attempts("CLM-1001") == []
    assert any("REFUSED" in str(t.output) for t in out.trace)


def test_llm_cannot_change_identity():
    s = svc()  # model tries to read someone else's claim through tool arguments
    m = ToolLLM([("get_status", {"claim_no": "CLM-2002", "policy_no": "POL-5002", "requester": "Sunita Rao"})])
    out = llm.run_agent(s, "x", "CLM-1001", "POL-5001", "Priya Kumar", True, m)
    assert out.status.claim_no == "CLM-1001"
    assert s.portal.action_log("CLM-2002") == []


def test_llm_tool_loop_blocked_by_consent():
    s = svc()
    m = ToolLLM([("get_status", {}), ("propose_upload", {"file_name": "final_invoice_CLM-2002.pdf"})])
    out = llm.run_agent(s, "x", "CLM-2002", "POL-5002", "Anil Rao", True, m)
    assert out.status.state == QueryState.CONSENT_REQUIRED and out.proposals == []
    assert s.portal.action_log() == []


def test_llm_proposal_for_wrong_file_is_rejected_by_code():
    s = svc()
    m = ToolLLM([("get_status", {}), ("propose_upload", {"file_name": "final_invoice_CLM-2002.pdf"})])
    out = llm.run_agent(s, "x", "CLM-1001", "POL-5001", "Priya Kumar", True, m)
    # the bad proposal was refused; the code's own mapper still proposes the correct file
    assert [p.file_name for p in out.proposals] == ["final_invoice_CLM-1001.pdf"]


def test_llm_message_with_invented_facts_is_replaced():
    s = svc()
    r = s.get_claim_status("CLM-3003", "POL-5003", "Meera Nair", False)
    msg, used, why = llm.phrase_result(r, ScriptedLLM("Your claim will be decided in 3 days."))
    assert used is False and "3" not in msg and "rejected" in why
    msg, used, why = llm.phrase_result(r, ScriptedLLM("Your claim is approved."))
    assert used is False and "approved" not in msg.lower()


def test_parse_request_validates_llm_output():
    good = ScriptedLLM(json.dumps({"claim_no": "clm-1001", "policy_no": "POL-5001", "requester": "Priya Kumar"}))
    p = llm.parse_request("where is my father's claim", good)
    assert (p.claim_no, p.policy_no, p.requester, p.source) == ("CLM-1001", "POL-5001", "Priya Kumar", "llm")
    evil = ScriptedLLM(json.dumps({"claim_no": "1; DROP TABLE", "policy_no": "x", "requester": "<script>"}))
    p = llm.parse_request("claim CLM-1234 please", evil)
    assert p.claim_no == "CLM-1234" and p.requester == ""  # regex fallback; bad name dropped
    assert llm.parse_request("see CLM-4004 and POL-5004", None).policy_no == "POL-5004"
