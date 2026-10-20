"""llm.py - the ONLY place that talks to an LLM.

Division of labour (see master prompt section 4):
  LLM : parse the question, choose the next tool, propose a file, phrase the result
  CODE: validates everything. Status, consent, missing docs, approval, upload and
        verification are never decided by the LLM.

The LLM can only call: get_status, list_documents, check_consent, propose_upload.
`execute_upload` is NOT exposed. Identity arguments (claim/policy/requester) are
always taken from the validated form fields, never from model output.

If the API key is missing, the call fails, times out, or returns something that
does not validate, everything falls back to a deterministic template flow.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import engine
from contract import (ProposalResult, QueryState, Status, StatusResult, TraceItem)
from service import ClaimService, NotAuthorized

ALLOWED_TOOLS = {"get_status", "list_documents", "check_consent", "propose_upload"}
MAX_STEPS = 6
DEFAULT_MODEL = os.environ.get("CLAIM_LLM_MODEL", "claude-sonnet-5-5")

TOOL_SPECS = [
    {"name": "check_consent", "description": "Check whether the requester may see this claim.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_status", "description": "Read the claim's status and missing documents from the portal.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_documents", "description": "List documents already on the claim.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "propose_upload", "description": "Prepare (NOT perform) an upload of one local file for human approval.",
     "input_schema": {"type": "object", "properties": {"file_name": {"type": "string"}},
                      "required": ["file_name"]}},
]
SYSTEM_PROMPT = (
    "You help a family member check an insurance claim. Use the tools to look up the claim. "
    "You cannot upload anything; you can only propose an upload for a human to approve. "
    "Never state or guess a claim status yourself - tools return it. Stop calling tools once "
    "you have the status, missing documents and, if useful, one proposal."
)


class LLMClient(Protocol):
    def chat(self, system: str, messages: list[dict], tools: Optional[list[dict]] = None) -> dict:
        """Return {"content": [ {"type":"text","text":..} | {"type":"tool_use","id","name","input"} ]}."""


class AnthropicClient:
    """Real client (needs `pip install anthropic` and ANTHROPIC_API_KEY)."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, timeout: float = 20.0):
        import anthropic  # imported lazily so the app runs without the package
        self._client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
                                           timeout=timeout, max_retries=1)
        self.model = model

    def chat(self, system, messages, tools=None) -> dict:
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=600, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)
        return {"content": [b.model_dump(exclude_none=True) for b in resp.content]}


def default_client() -> Optional[LLMClient]:
    """Returns a client when a key is configured, else None (-> template mode)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        try:  # Streamlit Cloud secrets
            import streamlit as st
            key = st.secrets.get("ANTHROPIC_API_KEY")  # type: ignore[attr-defined]
        except Exception:
            key = None
    if not key:
        return None
    try:
        return AnthropicClient(api_key=key)
    except Exception:
        return None


# ------------------------------------------------------------------ parsing
@dataclass
class ParsedRequest:
    claim_no: str = ""
    policy_no: str = ""
    requester: str = ""
    source: str = "template"


def _regex_parse(text: str) -> ParsedRequest:
    claim = re.search(r"\bCLM-\d{4,6}\b", text or "", re.I)
    policy = re.search(r"\bPOL-\d{4,6}\b", text or "", re.I)
    return ParsedRequest(claim.group(0).upper() if claim else "", policy.group(0).upper() if policy else "")


def parse_request(text: str, llm: Optional[LLMClient] = None) -> ParsedRequest:
    """LLM extracts fields; code validates the result. Falls back to regex."""
    base = _regex_parse(text)
    if not llm or not text.strip():
        return base
    try:
        out = llm.chat("Extract claim_no (CLM-####), policy_no (POL-####) and requester name from the "
                       "message. Reply with JSON only: {\"claim_no\":\"\",\"policy_no\":\"\",\"requester\":\"\"}.",
                       [{"role": "user", "content": text}])
        raw = "".join(b.get("text", "") for b in out["content"] if b.get("type") == "text")
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        claim = str(data.get("claim_no", "")).upper()
        policy = str(data.get("policy_no", "")).upper()
        name = " ".join(str(data.get("requester", "")).split())
        return ParsedRequest(claim if engine.CLAIM_RE.match(claim) else base.claim_no,
                             policy if engine.POLICY_RE.match(policy) else base.policy_no,
                             name if engine.NAME_RE.match(name) else "", "llm")
    except Exception:
        return base


# -------------------------------------------------------------------- agent
@dataclass
class AgentOutcome:
    status: StatusResult
    message: str
    proposals: list[ProposalResult] = field(default_factory=list)
    used_llm: bool = False
    llm_note: str = ""
    trace: list[TraceItem] = field(default_factory=list)


def _facts_text(res: StatusResult) -> str:
    return json.dumps({"claim": res.claim_no, "status": res.status.value, "raw": res.raw_status,
                       "missing": [engine.label(m) for m in res.missing_docs] + res.missing_docs,
                       "present": res.present_docs,  # (timestamps deliberately excluded)
                       "reason": res.reason, "next": res.next_step})


def phrase_result(res: StatusResult, llm: Optional[LLMClient]) -> tuple[str, bool, str]:
    """Plain-language message. LLM text is used only if it passes validate_message."""
    fallback = engine.template_message(res.status, res.state, res.missing_docs, res.reason)
    if not llm:
        return fallback, False, "template (no LLM configured)"
    try:
        out = llm.chat("Write 1-2 plain, kind sentences for a worried family member. Use ONLY these facts. "
                       "Do not add numbers, names or file names that are not in the facts. "
                       "Never imply approval unless the status is APPROVED.",
                       [{"role": "user", "content": _facts_text(res)}])
        msg = "".join(b.get("text", "") for b in out["content"] if b.get("type") == "text").strip()
    except Exception as exc:
        return fallback, False, f"template (LLM failed: {type(exc).__name__})"
    ok, why = engine.validate_message(msg, res.status, _facts_text(res))
    if not ok:
        return fallback, False, f"template (LLM message rejected: {why})"
    return msg, True, "LLM message passed validation"


def run_agent(service: ClaimService, question: str, claim_no: str, policy_no: str, requester: str,
              consent: bool, llm: Optional[LLMClient] = None) -> AgentOutcome:
    """PLAN -> TOOL CALL -> OBSERVE -> DECIDE (then the UI handles APPROVE -> ACT -> VERIFY)."""
    trace: list[TraceItem] = []
    note = ""

    def t(step, tool, inp, out):
        trace.append(TraceItem(step=step, tool=tool, input=inp, output=out))

    parsed = parse_request(question, llm)
    claim_no = claim_no.strip() or parsed.claim_no
    policy_no = policy_no.strip() or parsed.policy_no
    requester = requester.strip() or parsed.requester
    t("PLAN", "parse_request", question, {"claim_no": claim_no, "policy_no": policy_no,
                                          "requester": requester, "parsed_by": parsed.source})

    status: Optional[StatusResult] = None
    proposals: list[ProposalResult] = []
    used_llm_loop = False

    if llm:
        try:
            status, proposals = _llm_tool_loop(service, llm, question, claim_no, policy_no,
                                               requester, consent, trace)
            used_llm_loop = True
        except Exception as exc:  # any API/validation failure -> deterministic flow
            note = f"LLM loop failed ({type(exc).__name__}); used deterministic flow."
            t("DECIDE", "fallback", str(exc)[:120], "deterministic template flow")
            status, proposals = None, []

    if status is None:  # deterministic flow
        status = service.get_claim_status(claim_no, policy_no, requester, consent)
        trace.extend(status.trace)
        proposals = _auto_propose(service, status, trace)

    message, msg_llm, msg_note = phrase_result(status, llm)
    t("DECIDE", "phrase_result", None, msg_note)
    return AgentOutcome(status=status, message=message, proposals=proposals,
                        used_llm=used_llm_loop or msg_llm, llm_note=note or msg_note, trace=trace)


def _auto_propose(service: ClaimService, status: StatusResult, trace: list[TraceItem]) -> list[ProposalResult]:
    """Create proposals (NOT uploads) for missing docs that have exactly one matching file."""
    out: list[ProposalResult] = []
    if status.state != QueryState.OK or status.status != Status.DOCS_MISSING:
        return out
    cands = engine.candidate_files(status.claim_no, status.missing_docs, service.available_files())
    for doc_type, files in cands.items():
        if len(files) == 1:
            try:
                p = service.propose_upload(status.claim_no, files[0]["file_name"])
                out.append(p)
                trace.append(TraceItem(step="TOOL CALL", tool="propose_upload",
                                       input={"file_name": p.file_name},
                                       output={"proposal_id": p.proposal_id, "destination": p.destination_claim}))
            except (NotAuthorized, ValueError) as exc:
                trace.append(TraceItem(step="OBSERVE", tool="propose_upload", input=files[0]["file_name"],
                                       output=f"blocked: {exc}"))
        else:
            trace.append(TraceItem(step="DECIDE", tool="engine.candidate_files", input=doc_type,
                                   output=f"{len(files)} matching files -> human must choose"))
    return out


def _llm_tool_loop(service: ClaimService, llm: LLMClient, question: str, claim_no: str, policy_no: str,
                   requester: str, consent: bool, trace: list[TraceItem]):
    """Small tool-calling loop. Every tool is executed by CODE with validated identity."""
    status_holder: dict[str, StatusResult] = {}
    proposals: list[ProposalResult] = []

    def get_status() -> StatusResult:
        if "s" not in status_holder:
            status_holder["s"] = service.get_claim_status(claim_no, policy_no, requester, consent)
            trace.extend(status_holder["s"].trace)
        return status_holder["s"]

    def dispatch(name: str, args: dict) -> Any:
        if name not in ALLOWED_TOOLS:
            trace.append(TraceItem(step="DECIDE", tool=name, input=args,
                                   output="REFUSED: tool not permitted for the LLM"))
            return {"error": "tool not permitted"}
        if name in ("get_status", "check_consent"):
            s = get_status()
            return {"state": s.state.value, "status": s.status.value, "reason": s.reason,
                    "missing_docs": s.missing_docs}
        if name == "list_documents":
            s = get_status()
            return {"documents": s.present_docs} if s.state == QueryState.OK else {"error": "not authorised"}
        # propose_upload: validated by service (consent run, file exists, matches a missing doc)
        try:
            p = service.propose_upload(claim_no, str(args.get("file_name", "")))
            proposals.append(p)
            trace.append(TraceItem(step="TOOL CALL", tool="propose_upload", input=args,
                                   output={"proposal_id": p.proposal_id}))
            return {"proposal_id": p.proposal_id, "needs_human_approval": True}
        except (NotAuthorized, ValueError) as exc:
            trace.append(TraceItem(step="OBSERVE", tool="propose_upload", input=args, output=f"blocked: {exc}"))
            return {"error": str(exc)}

    files = [f["file_name"] for f in service.available_files()]
    messages: list[dict] = [{"role": "user", "content":
                             f"Question: {question or 'Where is this claim?'}\nAvailable local files: {files}"}]
    for _ in range(MAX_STEPS):
        out = llm.chat(SYSTEM_PROMPT, messages, TOOL_SPECS)
        calls = [b for b in out["content"] if b.get("type") == "tool_use"]
        if not calls:
            break
        messages.append({"role": "assistant", "content": out["content"]})
        results = []
        for c in calls:
            trace.append(TraceItem(step="TOOL CALL", tool=f"llm->{c['name']}", input=c.get("input")))
            res = dispatch(c["name"], c.get("input") or {})
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": json.dumps(res)})
        messages.append({"role": "user", "content": results})
    # the status always comes from code, even if the model never asked for it
    status = get_status()
    if status.state == QueryState.OK and status.status == Status.DOCS_MISSING and not proposals:
        proposals.extend(_auto_propose(service, status, trace))
    return status, proposals
