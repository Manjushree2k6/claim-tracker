"""portal.py - portal adapter.

`PortalAdapter` is the interface the rest of the app codes against.
`MockPortal` is our synthetic insurer portal (state lives in SQLite so it
survives Streamlit reruns). The organiser's mock portal plugs in by
implementing the same three methods (see README -> "Organiser portal").

ASSUMPTIONS (flagged, because we have not yet seen the organiser portal):
  * a claim exposes: raw status text, a status stamp, required doc types
  * a document list exposes: file name, doc type, sha256 per document
  * the portal does NOT de-duplicate uploads itself (so our code must)
  * after the last required document arrives a "Documents Pending" claim
    moves to "Under Review" and its status stamp changes
"""
from __future__ import annotations

import json
import os
from typing import Protocol

from contract import utc_now
from db import Database

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class PortalTimeout(Exception):
    """The portal did not answer in time (the write may or may not have landed)."""


class PortalError(Exception):
    """The portal answered with an error (e.g. unknown claim)."""


class PortalAdapter(Protocol):
    def get_claim(self, claim_no: str) -> dict: ...
    def list_documents(self, claim_no: str) -> dict: ...
    def upload_document(self, claim_no: str, file_name: str, sha256: str,
                        doc_type: str, idempotency_key: str) -> dict: ...


class MockPortal:
    """Synthetic insurer/TPA portal with fault injection for timeouts."""

    def __init__(self, db: Database, auto_seed: bool = True) -> None:
        self.db = db
        if auto_seed and not self.db.one("SELECT 1 AS x FROM portal_claims LIMIT 1"):
            self.seed()

    # -- seeding ---------------------------------------------------------
    def seed(self) -> None:
        """(Re)load synthetic claims, documents and fault settings."""
        for t in ("portal_claims", "portal_docs", "portal_faults", "portal_log"):
            self.db.execute(f"DELETE FROM {t}")
        with open(os.path.join(FIXTURES, "claims.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        for c in data["claims"]:
            self.db.execute(
                "INSERT INTO portal_claims(claim_no,raw_status,stamp_n,stamp_ts,required_json)"
                " VALUES(?,?,?,?,?)",
                (c["claim_no"], c["raw_status"], 1, utc_now(), json.dumps(c["required_docs"])))
            for d in c.get("present_docs", []):
                self.db.execute(
                    "INSERT INTO portal_docs(claim_no,file_name,doc_type,sha256,idem_key,uploaded_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (c["claim_no"], d["file_name"], d["doc_type"], d["sha256"], "seed", utc_now()))
            if c.get("fault"):
                self.db.execute("INSERT INTO portal_faults(claim_no,mode,remaining) VALUES(?,?,?)",
                                (c["claim_no"], c["fault"]["mode"], c["fault"]["times"]))

    # -- internals -------------------------------------------------------
    def _log(self, action: str, claim_no: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO portal_log(ts,action,claim_no,detail) VALUES(?,?,?,?)",
                        (utc_now(), action, claim_no, detail))

    def _claim_row(self, claim_no: str) -> dict:
        row = self.db.one("SELECT * FROM portal_claims WHERE claim_no=?", (claim_no,))
        if not row:
            raise PortalError(f"claim {claim_no} not found")
        return row

    @staticmethod
    def _stamp(row: dict) -> str:
        return f"STAMP-{row['claim_no']}-{row['stamp_n']:03d}@{row['stamp_ts']}"

    def _docs(self, claim_no: str) -> list[dict]:
        return self.db.query(
            "SELECT file_name,doc_type,sha256 FROM portal_docs WHERE claim_no=? ORDER BY id",
            (claim_no,))

    # -- adapter interface -----------------------------------------------
    def get_claim(self, claim_no: str) -> dict:
        row = self._claim_row(claim_no)
        self._log("GET_CLAIM", claim_no)
        return {
            "source_id": f"mock-portal://claims/{claim_no}",
            "retrieved_at": utc_now(),
            "claim_no": claim_no,
            "raw_status": row["raw_status"],
            "status_stamp": self._stamp(row),
            "required_docs": json.loads(row["required_json"]),
        }

    def list_documents(self, claim_no: str) -> dict:
        self._claim_row(claim_no)
        self._log("LIST_DOCS", claim_no)
        return {
            "source_id": f"mock-portal://claims/{claim_no}/documents",
            "retrieved_at": utc_now(),
            "documents": self._docs(claim_no),
        }

    def upload_document(self, claim_no: str, file_name: str, sha256: str,
                        doc_type: str, idempotency_key: str) -> dict:
        row = self._claim_row(claim_no)
        fault = self.db.one(
            "SELECT rowid AS rid, * FROM portal_faults WHERE claim_no=? AND remaining>0", (claim_no,))
        if fault:
            self.db.execute("UPDATE portal_faults SET remaining=remaining-1 WHERE rowid=?",
                            (fault["rid"],))
        mode = fault["mode"] if fault else None
        if mode == "timeout_before_commit":
            self._log("UPLOAD", claim_no, f"{file_name} key={idempotency_key} -> TIMEOUT (not stored)")
            raise PortalTimeout("portal timed out before storing the file")
        self._store(claim_no, row, file_name, doc_type, sha256, idempotency_key)
        if mode == "timeout_after_commit":
            self._log("UPLOAD", claim_no,
                      f"{file_name} key={idempotency_key} -> STORED but response TIMED OUT")
            raise PortalTimeout("portal timed out after storing the file")
        self._log("UPLOAD", claim_no, f"{file_name} key={idempotency_key} -> OK")
        return {"source_id": f"mock-portal://claims/{claim_no}/documents",
                "retrieved_at": utc_now(), "stored": True}

    def _store(self, claim_no, row, file_name, doc_type, sha256, idem_key) -> None:
        self.db.execute(
            "INSERT INTO portal_docs(claim_no,file_name,doc_type,sha256,idem_key,uploaded_at)"
            " VALUES(?,?,?,?,?,?)", (claim_no, file_name, doc_type, sha256, idem_key, utc_now()))
        required = set(json.loads(row["required_json"]))
        have = {d["doc_type"] for d in self._docs(claim_no)}
        if required <= have and row["raw_status"].strip().lower() == "documents pending":
            self.db.execute(
                "UPDATE portal_claims SET raw_status='Under Review', stamp_n=stamp_n+1, stamp_ts=?"
                " WHERE claim_no=?", (utc_now(), claim_no))

    # -- test / audit helpers (not part of the adapter interface) ---------
    def action_log(self, claim_no: str | None = None) -> list[dict]:
        if claim_no:
            return self.db.query("SELECT * FROM portal_log WHERE claim_no=? ORDER BY id", (claim_no,))
        return self.db.query("SELECT * FROM portal_log ORDER BY id")

    def upload_attempts(self, claim_no: str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM portal_log WHERE claim_no=? AND action='UPLOAD' ORDER BY id", (claim_no,))
