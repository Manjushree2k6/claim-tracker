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
import time
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
    provider = "Anthropic"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, timeout: float = 20.0):
        import anthropic  # imported lazily so the app runs without the package
        self._client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
                                           timeout=timeout, max_retries=1)
        self.model = model
        self.label = f"Anthropic ({model})"

    def chat(self, system, messages, tools=None) -> dict:
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=600, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)
        return {"content": [b.model_dump(exclude_none=True) for b in resp.content]}


GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _gemini_schema(schema: dict) -> dict:
    """JSON-schema (as used in TOOL_SPECS) -> Gemini function-parameter schema (upper-case types)."""
    out: dict[str, Any] = {}
    if "type" in schema:
        out["type"] = str(schema["type"]).upper()
    if schema.get("description"):
        out["description"] = schema["description"]
    if schema.get("enum"):
        out["enum"] = schema["enum"]
    if schema.get("properties"):
        out["properties"] = {k: _gemini_schema(v) for k, v in schema["properties"].items()}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    if schema.get("items"):
        out["items"] = _gemini_schema(schema["items"])
    return out


class GeminiClient:
    """Google Gemini over plain REST (needs `requests`, which Streamlit already installs).

    It speaks the same neutral message format as AnthropicClient, so the agent loop is untouched:
    messages in -> Gemini `contents`; Gemini parts out -> {"type": "text" | "tool_use"} blocks.
    The raw Gemini part is kept on each block (`_gemini_part`) and echoed back verbatim, which
    preserves Gemini's "thought signatures" between tool-calling turns.
    """
    provider = "Gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 25.0,
                 http_post=None):
        self._key = api_key or os.environ["GEMINI_API_KEY"]
        self.model = model or GEMINI_DEFAULT_MODEL
        self.timeout = timeout
        self._post = http_post  # injectable for tests
        self.label = f"Gemini ({self.model})"

    # ---- request building
    @staticmethod
    def _decl(tool: dict) -> dict:
        decl: dict[str, Any] = {"name": tool["name"], "description": tool.get("description", "")}
        schema = tool.get("input_schema") or {}
        if schema.get("properties"):  # Gemini rejects an OBJECT with no properties
            decl["parameters"] = _gemini_schema(schema)
        return decl

    @staticmethod
    def _contents(messages: list[dict]) -> list[dict]:
        contents: list[dict] = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            c = m["content"]
            if isinstance(c, str):
                parts = [{"text": c}]
            else:
                parts = []
                for b in c:
                    if "_gemini_part" in b:
                        parts.append(b["_gemini_part"])
                    elif b.get("type") == "text":
                        parts.append({"text": b.get("text", "")})
                    elif b.get("type") == "tool_use":
                        parts.append({"functionCall": {"name": b["name"], "args": b.get("input") or {}}})
                    elif b.get("type") == "tool_result":
                        name, _, rest = str(b.get("tool_use_id", "")).partition("#")
                        try:
                            payload = json.loads(b.get("content") or "{}")
                        except (TypeError, ValueError):
                            payload = {"result": b.get("content")}
                        if not isinstance(payload, dict):
                            payload = {"result": payload}
                        fr: dict[str, Any] = {"name": name, "response": payload}
                        if rest.startswith("id:"):
                            fr["id"] = rest[3:]
                        parts.append({"functionResponse": fr})
            contents.append({"role": role, "parts": parts})
        return contents

    # ---- transport
    def _send(self, body: dict) -> dict:
        if self._post is None:
            import requests  # lazy: the app runs without it in template mode
            self._post = requests.post
        url = _GEMINI_URL.format(model=self.model)
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        resp = None
        for attempt in range(2):  # one retry on rate-limit / transient server errors
            resp = self._post(url, headers=headers, json=body, timeout=self.timeout)
            if resp.status_code in (429, 500, 503) and attempt == 0:
                time.sleep(1.5)
                continue
            break
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {resp.status_code}: {str(resp.text)[:200]}")
        return resp.json()

    # ---- public API (same contract as AnthropicClient.chat)
    def chat(self, system, messages, tools=None) -> dict:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._contents(messages),
            "generationConfig": {"maxOutputTokens": 2048},  # headroom: thinking tokens count too
        }
        if tools:
            body["tools"] = [{"functionDeclarations": [self._decl(t) for t in tools]}]
        data = self._send(body)
        cands = data.get("candidates") or []
        if not cands:
            raise RuntimeError("Gemini returned no candidate (blocked or empty response)")
        blocks: list[dict] = []
        for i, part in enumerate((cands[0].get("content") or {}).get("parts") or []):
            if part.get("thought"):
                blocks.append({"type": "thought", "_gemini_part": part})  # echoed back, never shown
            elif "functionCall" in part:
                fc = part["functionCall"]
                call_id = f"{fc['name']}#id:{fc['id']}" if fc.get("id") else f"{fc['name']}#n{i}"
                blocks.append({"type": "tool_use", "id": call_id, "name": fc["name"],
                               "input": fc.get("args") or {}, "_gemini_part": part})
            elif "text" in part:
                blocks.append({"type": "text", "text": part["text"], "_gemini_part": part})
        return {"content": blocks}


def _secret(name: str) -> Optional[str]:
    """Environment variable first, then Streamlit secrets (local secrets.toml or Cloud settings)."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st
        val = st.secrets.get(name)  # type: ignore[attr-defined]
    except Exception:
        val = None
    return str(val) if val else None


def default_client() -> Optional[LLMClient]:
    """Returns a client when a key is configured, else None (-> template mode).

    Gemini is used when GEMINI_API_KEY (or GOOGLE_API_KEY) is set; otherwise Anthropic when
    ANTHROPIC_API_KEY is set. Set LLM_PROVIDER=anthropic to prefer Anthropic when both exist.
    """
    gkey = _secret("GEMINI_API_KEY") or _secret("GOOGLE_API_KEY")
    akey = _secret("ANTHROPIC_API_KEY")
    order = ["anthropic", "gemini"] if (_secret("LLM_PROVIDER") or "").lower() == "anthropic" \
        else ["gemini", "anthropic"]
    for provider in order:
        try:
            if provider == "gemini" and gkey:
                return GeminiClient(api_key=gkey, model=_secret("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL)
            if provider == "anthropic" and akey:
                return AnthropicClient(api_key=akey)
        except Exception:
            continue
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

    # Form fields always win over parsed ones, so only spend an AI call on parsing when a field is empty.
    all_given = bool(claim_no.strip() and policy_no.strip() and requester.strip())
    parsed = parse_request(question, None if all_given else llm)
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