"""service.py - implements the five contract functions.

Flow: get_claim_status -> propose_upload -> (human approves) -> execute_upload
-> verify_upload. The executor lives here; the Verifier lives in engine.py and
shares no code with it.
"""
from __future__ import annotations

import os
import uuid

import engine
from contract import (Evidence, ExecutionResult, ExecutionState, ProposalResult,
                      QueryState, Status, StatusResult, TestCaseResult, TraceItem,
                      VerificationResult)
from db import Database
from portal import MockPortal, PortalAdapter, PortalError, PortalTimeout


class NotAuthorized(Exception):
    """Raised when an action is attempted without a passed consent check."""


class ClaimService:
    def __init__(self, db: Database | None = None, portal: PortalAdapter | None = None,
                 files_dir: str | None = None) -> None:
        self.db = db or Database(os.environ.get("CLAIM_DB", "data/app.db"))
        self.portal = portal or MockPortal(self.db)
        self.registry = engine.Registry()
        self.files_dir = files_dir
        self.verifier = engine.Verifier(self.db, self.portal)
        self._seed_consents()

    @classmethod
    def fresh(cls) -> "ClaimService":
        """Isolated in-memory service (used by test cases so the demo DB is untouched)."""
        return cls(db=Database(":memory:"))

    def _seed_consents(self) -> None:
        for c in self.registry.consents:
            if not self.db.consent_for(c["claim_no"], c["grantee"]):
                self.db.add_consent(c["claim_no"], c["grantee"], c["granted_by"])

    def reset_demo(self) -> None:
        """Reload synthetic portal data and clear runs/approvals (keeps test results)."""
        for t in ("runs", "proposals", "approvals", "executions", "verifications", "consents"):
            self.db.execute(f"DELETE FROM {t}")
        if isinstance(self.portal, MockPortal):
            self.portal.seed()
        self._seed_consents()

    def record_holder_consent(self, claim_no: str, grantee: str) -> None:
        """SIMULATED holder action: the policy holder grants `grantee` access (demo only)."""
        holder = next((p["holder"] for p in self.registry.policies.values()
                       if p["claim_no"] == claim_no), "policy holder")
        self.db.add_consent(claim_no, " ".join(grantee.split()), holder)

    def available_files(self) -> list[dict]:
        return engine.list_available_files(self.files_dir)

    # ------------------------------------------------------------ 1. status
    def get_claim_status(self, claim_no: str, policy_no: str, requester: str,
                         consent: bool) -> StatusResult:
        claim_no = (claim_no or "").strip().upper()
        policy_no = (policy_no or "").strip().upper()
        requester = " ".join((requester or "").split())
        trace: list[TraceItem] = []

        def t(step: str, tool: str, inp, out) -> None:
            trace.append(TraceItem(step=step, tool=tool, input=inp, output=out))

        def done(res: StatusResult) -> StatusResult:
            res.trace, res.claim_no = trace, claim_no
            res.run_id = self.db.save_run(claim_no, policy_no, requester, consent, res)
            return res

        problems = engine.validate_inputs(claim_no, policy_no, requester)
        t("PLAN", "validate_inputs", {"claim_no": claim_no, "policy_no": policy_no,
                                      "requester": requester}, problems or "inputs valid")
        if problems:
            return done(StatusResult(state=QueryState.UNCERTAIN, status=Status.UNKNOWN,
                                     reason=" ".join(problems), rule_fired="R-input-invalid",
                                     next_step="Correct the details and try again."))

        auth = self.registry.check_authorization(self.db, claim_no, policy_no, requester, consent)
        t("TOOL CALL", "check_consent", {"requester": requester, "consent_checkbox": consent},
          {"authorized": auth.authorized, "rule": auth.rule, "reason": auth.reason})
        if not auth.authorized:
            t("DECIDE", "engine", auth.rule, "stop before any portal action")
            nxt = ("Ask the policy holder to give consent for you, then try again."
                   if auth.state == QueryState.CONSENT_REQUIRED
                   else "Correct the numbers and try again.")
            return done(StatusResult(state=auth.state, status=Status.UNKNOWN, reason=auth.reason,
                                     rule_fired=auth.rule, next_step=nxt))

        try:
            claim = self.portal.get_claim(claim_no)
            t("TOOL CALL", "portal.get_claim", {"claim_no": claim_no},
              {"raw_status": claim["raw_status"], "status_stamp": claim["status_stamp"]})
            docs = self.portal.list_documents(claim_no)
            t("TOOL CALL", "portal.list_documents", {"claim_no": claim_no},
              [d["file_name"] for d in docs["documents"]])
        except (PortalTimeout, PortalError) as exc:
            t("OBSERVE", "portal", None, f"portal problem: {exc}")
            return done(StatusResult(
                state=QueryState.UNCERTAIN, status=Status.UNKNOWN, rule_fired="R-portal-unavailable",
                reason=f"The insurer portal did not give a usable answer ({exc}).",
                next_step="Manual verification required: try again later or contact the insurer."))

        raw = claim["raw_status"]
        status = engine.map_status(raw)
        present_types = {d["doc_type"] for d in docs["documents"]}
        missing = engine.compute_missing(claim["required_docs"], present_types)
        t("DECIDE", "engine.map_status", raw, status.value)
        t("DECIDE", "engine.compute_missing",
          {"required": claim["required_docs"], "present": sorted(present_types)}, missing)

        evidence = [
            Evidence(source_id_or_url=claim["source_id"], retrieved_at=claim["retrieved_at"],
                     field="raw_status", value=raw),
            Evidence(source_id_or_url=claim["source_id"], retrieved_at=claim["retrieved_at"],
                     field="status_stamp", value=claim["status_stamp"]),
            Evidence(source_id_or_url=claim["source_id"], retrieved_at=claim["retrieved_at"],
                     field="required_docs", value=claim["required_docs"]),
            Evidence(source_id_or_url=docs["source_id"], retrieved_at=docs["retrieved_at"],
                     field="documents_on_file", value=[d["file_name"] for d in docs["documents"]]),
        ]
        base = dict(evidence=evidence, raw_status=raw, status_stamp=claim["status_stamp"],
                    present_docs=[d["file_name"] for d in docs["documents"]])

        if status == Status.UNKNOWN:
            return done(StatusResult(
                state=QueryState.UNCERTAIN, status=Status.UNKNOWN, missing_docs=missing,
                rule_fired="R2-unmapped-status", reason=f"The portal shows '{raw}', which we do not recognise.",
                next_step=engine.next_step(Status.UNKNOWN, missing, {}, None), **base))
        if status == Status.DOCS_MISSING and not missing:
            return done(StatusResult(
                state=QueryState.UNCERTAIN, status=Status.UNKNOWN, rule_fired="R10-inconsistent",
                reason="The portal says documents are missing, but its list shows every required "
                       "document. We will not guess.",
                next_step="Manual verification required: contact the insurer/TPA.", **base))

        cands = engine.candidate_files(claim_no, missing, self.available_files())
        t("DECIDE", "engine.candidate_files", missing,
          {k: [f["file_name"] for f in v] for k, v in cands.items()})
        reason = f"Portal status '{raw}' mapped to {status.value}."
        if status != Status.DOCS_MISSING and missing:
            reason += " Some required documents are also not on file."
        return done(StatusResult(
            state=QueryState.OK, status=status, missing_docs=missing, rule_fired=f"R1-portal-status:{status.value}",
            reason=reason, next_step=engine.next_step(status, missing, cands, claim["status_stamp"]),
            **base))

    # ---------------------------------------------------------- 2. proposal
    def propose_upload(self, claim_no: str, file_name: str) -> ProposalResult:
        claim_no = (claim_no or "").strip().upper()
        run = self.db.latest_ok_run(claim_no)
        if not run:
            raise NotAuthorized("No passed consent check for this claim. No portal action allowed.")
        file = next((f for f in self.available_files() if f["file_name"] == file_name), None)
        if not file:
            raise ValueError(f"'{file_name}' is not one of the available files.")
        claim = self.portal.get_claim(claim_no)
        docs = self.portal.list_documents(claim_no)["documents"]
        missing = engine.compute_missing(claim["required_docs"], {d["doc_type"] for d in docs})
        doc_type = next((m for m in missing if engine.file_matches(m, file_name, claim_no)), None)
        if not doc_type:
            raise ValueError("This file does not match any document that is missing on this claim.")
        key = engine.idempotency_key(claim_no, file["sha256"])
        existing = self.db.pending_proposal(claim_no, file["sha256"])
        if existing:
            return self._proposal_result(existing)
        pid = "PROP-" + uuid.uuid4().hex[:8].upper()
        summary = (f"Upload '{file_name}' as '{engine.label(doc_type)}' to claim {claim_no}. "
                   f"The claim's document list goes from {len(docs)} to {len(docs) + 1} files. "
                   f"File fingerprint (sha256): {file['sha256'][:16]}... "
                   "Once uploaded, the insurer holds this file and it cannot easily be removed.")
        self.db.save_proposal(id=pid, claim_no=claim_no, file_name=file_name, doc_type=doc_type,
                              sha256=file["sha256"], idem_key=key, summary=summary,
                              run_id=run["id"])
        return self._proposal_result(self.db.get_proposal(pid))

    @staticmethod
    def _proposal_result(row: dict) -> ProposalResult:
        return ProposalResult(proposal_id=row["id"], file_name=row["file_name"], sha256=row["sha256"],
                              destination_claim=row["claim_no"], change_summary=row["summary"],
                              doc_type=row["doc_type"], idempotency_key=row["idem_key"])

    # ----------------------------------------------------------- 3. execute
    def execute_upload(self, proposal_id: str, approved: bool) -> ExecutionResult:
        """The ONLY code path that writes to the portal. Needs a stored approval record."""
        prop = self.db.get_proposal(proposal_id)
        if not prop:
            return ExecutionResult(state=ExecutionState.FAILED, idempotency_key="",
                                   reason="Unknown proposal. Nothing was uploaded.")
        key, claim_no, sha = prop["idem_key"], prop["claim_no"], prop["sha256"]

        if prop["status"] == "REJECTED":
            return ExecutionResult(state=ExecutionState.FAILED, idempotency_key=key,
                                   reason="This proposal was rejected. Nothing was uploaded.")
        done_before = self.db.completed_execution_by_key(key)
        if done_before:
            return ExecutionResult(state=ExecutionState.SKIPPED_DUPLICATE, idempotency_key=key,
                                   reason="Already uploaded earlier (same idempotency key). "
                                          "No second upload was made.")
        if not approved:
            self.db.save_approval(proposal_id, "human (UI)", "REJECTED", sha, claim_no)
            self.db.set_proposal_status(proposal_id, "REJECTED")
            self.db.save_execution(proposal_id, claim_no, key, "FAILED", 0, [],
                                   "Rejected by human. No action taken.", [])
            return ExecutionResult(state=ExecutionState.FAILED, idempotency_key=key,
                                   reason="Rejected by the human reviewer. No action was taken.")

        # approval record is stored BEFORE any portal write (rule 4)
        self.db.save_approval(proposal_id, "human (UI)", "APPROVED", sha, claim_no)
        if not self.db.latest_ok_run(claim_no):
            return ExecutionResult(state=ExecutionState.FAILED, idempotency_key=key,
                                   reason="No passed consent check for this claim. Nothing uploaded.")
        file = next((f for f in self.available_files() if f["file_name"] == prop["file_name"]), None)
        if not file or file["sha256"] != sha:
            return ExecutionResult(state=ExecutionState.FAILED, idempotency_key=key,
                                   reason="The file changed after it was approved. Nothing uploaded; "
                                          "please create a new proposal.")
        return self._run_upload(prop, file)

    def _run_upload(self, prop: dict, file: dict) -> ExecutionResult:
        claim_no, key, sha = prop["claim_no"], prop["idem_key"], prop["sha256"]
        log: list[str] = []
        try:
            before = self.portal.list_documents(claim_no)["documents"]
        except (PortalTimeout, PortalError) as exc:
            reason = f"Could not read the document list first ({exc}). Nothing uploaded."
            self.db.save_execution(prop["id"], claim_no, key, "TIMEOUT", 0, [], reason, log)
            return ExecutionResult(state=ExecutionState.TIMEOUT, idempotency_key=key, reason=reason)
        log.append(f"pre-check: {len(before)} documents on claim")
        before_rec = [{"file_name": d["file_name"], "sha256": d["sha256"]} for d in before]

        if any(d["sha256"] == sha for d in before):
            log.append("file already on claim -> skip upload")
            state, reason, attempts = ExecutionState.SKIPPED_DUPLICATE, "File was already on the claim.", 0
        else:
            state, reason, attempts = ExecutionState.TIMEOUT, "", 0
            for attempt in (1, 2):  # first try + AT MOST ONE retry, same key
                attempts = attempt
                try:
                    self.portal.upload_document(claim_no, prop["file_name"], sha, prop["doc_type"], key)
                    log.append(f"attempt {attempt}: upload OK")
                    state, reason = ExecutionState.EXECUTED, "Uploaded."
                    break
                except PortalTimeout:
                    log.append(f"attempt {attempt}: TIMEOUT -> re-reading document list before any retry")
                    try:
                        now = self.portal.list_documents(claim_no)["documents"]
                    except (PortalTimeout, PortalError):
                        log.append("could not re-read the list -> stop, leave to verification")
                        reason = "Timed out and the list could not be re-read. Not retried."
                        break
                    if any(d["sha256"] == sha for d in now):
                        log.append("file IS in the list -> do NOT upload again")
                        state, reason = (ExecutionState.SKIPPED_DUPLICATE,
                                         "Upload timed out but the file had already arrived. Not re-uploaded.")
                        break
                    log.append("file NOT in the list" + (" -> retry once, same key" if attempt == 1 else ""))
                    reason = "Timed out twice; file still not confirmed." if attempt == 2 else ""
                except PortalError as exc:
                    state, reason = ExecutionState.FAILED, f"Portal rejected the upload: {exc}"
                    break
        self.db.save_execution(prop["id"], claim_no, key, state.value, attempts, before_rec, reason, log)
        if state in (ExecutionState.EXECUTED, ExecutionState.SKIPPED_DUPLICATE):
            self.db.set_proposal_status(prop["id"], "DONE")
        return ExecutionResult(state=state, idempotency_key=key, reason=reason, attempts=attempts, log=log)

    # ------------------------------------------------------------ 4. verify
    def verify_upload(self, claim_no: str, file_name: str) -> VerificationResult:
        return self.verifier.verify((claim_no or "").strip().upper(), file_name)

    # ------------------------------------------------------- 5. test cases
    def run_test_case(self, tc_id: str) -> TestCaseResult:
        import testcases  # local import avoids a cycle
        res = testcases.run(tc_id)
        self.db.save_test_result(tc_id, res.passed, res.model_dump_json())
        return res
