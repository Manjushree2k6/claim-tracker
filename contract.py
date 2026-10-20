"""contract.py - shared enums, models and function signatures (FROZEN).

Any change to names, enum values or required fields must be announced as
"CONTRACT CHANGE:". Extra *optional* fields marked "(additive)" were added so
the UI can show more detail; they do not alter the frozen fields.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field


def utc_now() -> str:
    """ISO-8601 UTC timestamp used for every evidence/trace item."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Status(str, Enum):
    APPROVED = "APPROVED"
    UNDER_REVIEW = "UNDER_REVIEW"
    REJECTED = "REJECTED"
    DOCS_MISSING = "DOCS_MISSING"
    UNKNOWN = "UNKNOWN"


class QueryState(str, Enum):
    OK = "OK"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    UNCERTAIN = "UNCERTAIN"


class ExecutionState(str, Enum):
    EXECUTED = "EXECUTED"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


class VerificationState(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    PENDING = "PENDING"


class Evidence(BaseModel):
    source_id_or_url: str
    retrieved_at: str
    field: str
    value: Any


class TraceItem(BaseModel):
    step: str  # PLAN | TOOL CALL | OBSERVE | DECIDE | APPROVE | ACT | VERIFY
    tool: str
    input: Any = None
    output: Any = None
    timestamp: str = Field(default_factory=utc_now)


class StatusResult(BaseModel):
    state: QueryState
    status: Status
    missing_docs: list[str] = Field(default_factory=list)
    next_step: str = ""
    reason: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    trace: list[TraceItem] = Field(default_factory=list)
    # (additive)
    rule_fired: str = ""
    run_id: Optional[int] = None
    claim_no: str = ""
    raw_status: Optional[str] = None
    status_stamp: Optional[str] = None
    present_docs: list[str] = Field(default_factory=list)


class ProposalResult(BaseModel):
    proposal_id: str
    file_name: str
    sha256: str
    destination_claim: str
    change_summary: str
    # (additive)
    doc_type: str = ""
    idempotency_key: str = ""


class ExecutionResult(BaseModel):
    state: ExecutionState
    idempotency_key: str
    # (additive)
    reason: str = ""
    attempts: int = 0
    log: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    state: VerificationState
    docs_before: list[str] = Field(default_factory=list)
    docs_after: list[str] = Field(default_factory=list)
    status_stamp: Optional[str] = None
    # (additive)
    reason: str = ""
    copies_found: int = 0


class TestCaseResult(BaseModel):
    __test__ = False  # not a pytest class
    input: dict
    expected: str
    actual: str
    evidence: list[Any] = Field(default_factory=list)
    passed: bool
    explanation: str
    # (additive)
    tc_id: str = ""
    checks: list[dict] = Field(default_factory=list)
    trace: list[TraceItem] = Field(default_factory=list)
    portal_log: list[dict] = Field(default_factory=list)


class ClaimServiceAPI(Protocol):
    """The five contract functions (implemented by service.ClaimService)."""

    def get_claim_status(self, claim_no: str, policy_no: str, requester: str, consent: bool) -> StatusResult: ...
    def propose_upload(self, claim_no: str, file_name: str) -> ProposalResult: ...
    def execute_upload(self, proposal_id: str, approved: bool) -> ExecutionResult: ...
    def verify_upload(self, claim_no: str, file_name: str) -> VerificationResult: ...
    def run_test_case(self, tc_id: str) -> TestCaseResult: ...
