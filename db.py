"""db.py - SQLite storage: runs, proposals, approvals, executions, verifications,
test results, consents, plus the mock portal's own tables.

Everything the UI needs to survive a Streamlit rerun lives here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Iterable

from contract import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, claim_no TEXT, policy_no TEXT,
  requester TEXT, consent_flag INTEGER, state TEXT, status TEXT, rule_fired TEXT,
  reason TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS proposals(
  id TEXT PRIMARY KEY, ts TEXT, claim_no TEXT, file_name TEXT, doc_type TEXT,
  sha256 TEXT, idem_key TEXT, summary TEXT, status TEXT, run_id INTEGER);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, proposal_id TEXT, approver TEXT,
  decision TEXT, file_sha256 TEXT, destination_claim TEXT);
CREATE TABLE IF NOT EXISTS executions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, proposal_id TEXT, claim_no TEXT,
  idem_key TEXT, state TEXT, attempts INTEGER, docs_before_json TEXT,
  reason TEXT, log_json TEXT);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, claim_no TEXT, file_name TEXT,
  state TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS test_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tc_id TEXT, passed INTEGER,
  result_json TEXT);
CREATE TABLE IF NOT EXISTS consents(
  claim_no TEXT, grantee TEXT, granted_by TEXT, ts TEXT,
  PRIMARY KEY(claim_no, grantee));
-- mock insurer portal -------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_claims(
  claim_no TEXT PRIMARY KEY, raw_status TEXT, stamp_n INTEGER, stamp_ts TEXT,
  required_json TEXT);
CREATE TABLE IF NOT EXISTS portal_docs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, claim_no TEXT, file_name TEXT,
  doc_type TEXT, sha256 TEXT, idem_key TEXT, uploaded_at TEXT);
CREATE TABLE IF NOT EXISTS portal_faults(
  claim_no TEXT, mode TEXT, remaining INTEGER);
CREATE TABLE IF NOT EXISTS portal_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, claim_no TEXT,
  detail TEXT);
"""


class Database:
    """Thin, thread-safe wrapper around one SQLite file (or :memory:)."""

    def __init__(self, path: str = "data/app.db") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur.lastrowid or 0

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- runs ------------------------------------------------------------
    def save_run(self, claim_no, policy_no, requester, consent, result) -> int:
        return self.execute(
            "INSERT INTO runs(ts,claim_no,policy_no,requester,consent_flag,state,status,"
            "rule_fired,reason,result_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (utc_now(), claim_no, policy_no, requester, int(bool(consent)),
             result.state.value, result.status.value, result.rule_fired, result.reason,
             result.model_dump_json()),
        )

    def latest_ok_run(self, claim_no: str) -> dict | None:
        return self.one(
            "SELECT * FROM runs WHERE claim_no=? AND state='OK' ORDER BY id DESC LIMIT 1",
            (claim_no,),
        )

    # -- proposals / approvals / executions -------------------------------
    def save_proposal(self, **c) -> None:
        self.execute(
            "INSERT INTO proposals(id,ts,claim_no,file_name,doc_type,sha256,idem_key,"
            "summary,status,run_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (c["id"], utc_now(), c["claim_no"], c["file_name"], c["doc_type"], c["sha256"],
             c["idem_key"], c["summary"], "PENDING", c.get("run_id")),
        )

    def get_proposal(self, pid: str) -> dict | None:
        return self.one("SELECT * FROM proposals WHERE id=?", (pid,))

    def set_proposal_status(self, pid: str, status: str) -> None:
        self.execute("UPDATE proposals SET status=? WHERE id=?", (status, pid))

    def pending_proposal(self, claim_no: str, sha256: str) -> dict | None:
        return self.one(
            "SELECT * FROM proposals WHERE claim_no=? AND sha256=? AND status='PENDING' "
            "ORDER BY ts DESC LIMIT 1", (claim_no, sha256))

    def latest_proposal_for(self, claim_no: str, file_name: str) -> dict | None:
        return self.one(
            "SELECT * FROM proposals WHERE claim_no=? AND file_name=? "
            "ORDER BY rowid DESC LIMIT 1", (claim_no, file_name))

    def save_approval(self, proposal_id, approver, decision, sha, claim) -> int:
        return self.execute(
            "INSERT INTO approvals(ts,proposal_id,approver,decision,file_sha256,"
            "destination_claim) VALUES(?,?,?,?,?,?)",
            (utc_now(), proposal_id, approver, decision, sha, claim))

    def approvals_for(self, proposal_id: str) -> list[dict]:
        return self.query("SELECT * FROM approvals WHERE proposal_id=? ORDER BY id", (proposal_id,))

    def save_execution(self, proposal_id, claim_no, idem_key, state, attempts,
                       docs_before, reason, log) -> int:
        return self.execute(
            "INSERT INTO executions(ts,proposal_id,claim_no,idem_key,state,attempts,"
            "docs_before_json,reason,log_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (utc_now(), proposal_id, claim_no, idem_key, state, attempts,
             json.dumps(docs_before), reason, json.dumps(log)))

    def latest_execution(self, proposal_id: str) -> dict | None:
        return self.one("SELECT * FROM executions WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
                        (proposal_id,))

    def completed_execution_by_key(self, idem_key: str) -> dict | None:
        return self.one(
            "SELECT * FROM executions WHERE idem_key=? AND state IN "
            "('EXECUTED','SKIPPED_DUPLICATE') ORDER BY id DESC LIMIT 1", (idem_key,))

    def save_verification(self, claim_no, file_name, result) -> int:
        return self.execute(
            "INSERT INTO verifications(ts,claim_no,file_name,state,result_json) VALUES(?,?,?,?,?)",
            (utc_now(), claim_no, file_name, result.state.value, result.model_dump_json()))

    def save_test_result(self, tc_id: str, passed: bool, result_json: str) -> int:
        return self.execute(
            "INSERT INTO test_results(ts,tc_id,passed,result_json) VALUES(?,?,?,?)",
            (utc_now(), tc_id, int(passed), result_json))

    def latest_test_results(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.query("SELECT * FROM test_results ORDER BY id"):
            out[r["tc_id"]] = r
        return out

    # -- consents (recorded authorisations) -------------------------------
    def add_consent(self, claim_no: str, grantee: str, granted_by: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO consents(claim_no,grantee,granted_by,ts) VALUES(?,?,?,?)",
            (claim_no, grantee, granted_by, utc_now()))

    def consent_for(self, claim_no: str, grantee: str) -> dict | None:
        return self.one("SELECT * FROM consents WHERE claim_no=? AND grantee=?",
                        (claim_no, grantee))
