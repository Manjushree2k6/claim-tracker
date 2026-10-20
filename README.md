# Insurance Claim Status Tracker

An agentic AI assistant that answers a family member's question, *"Where is my father's claim?"*, with a clear **status**, a **missing-document list** and a **next step**. With explicit human approval it uploads the right document to the right claim, and an independent verifier then re-reads the insurer portal to prove the result.

**Ziro National Agentic AI Hackathon 2026** · Healthcare & Wellness AI · Team V3

**Live demo:** (https://claim-tracker-v3.streamlit.app/)

> All data is synthetic. The agent never bypasses OTP or CAPTCHA.

---

## What it does

1. **Collect:** claim number, policy number, requester and consent.
2. **Plan and find:** reads the real status from the portal and maps document requests to the available files.
3. **Approve:** shows the exact file, destination claim and change, and waits for a human decision.
4. **Act:** uploads only after an approval record is stored.
5. **Verify:** a separate module re-reads the document list and status stamp and compares them with what was intended.
6. **Report:** status, missing items and next step, each traceable to its evidence.

## How it works

The agent loop is **PLAN, TOOL CALL, OBSERVE, DECIDE, APPROVE, ACT, VERIFY**. Every step is shown with a timestamp in the Agent trace panel.

| Component | Responsibility |
|---|---|
| **LLM (optional)** | Parses the question, chooses the next tool, suggests a file match, words the final message |
| **Code (always)** | Validates input, maps status, checks consent, computes missing documents, enforces approval, uploads, prevents duplicates, verifies |

The model can call only `get_status`, `list_documents`, `check_consent` and `propose_upload`. `execute_upload` is not exposed to it. Identity (claim, policy, requester) always comes from the validated form, never from model output. A model message is discarded and replaced by a template if it uses approval-style wording for a non-approved status or adds IDs or numbers that are not in the tool results. If the API fails, a deterministic template flow runs, so the app keeps working.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py          # open the URL it prints
python run_tests.py           # or: pytest -q  (39 tests)
python make_evidence.py       # regenerate /evidence from fresh test runs
```

No API key is needed. Without one the agent runs its deterministic flow, and all four test cases pass. To enable the optional AI assistant, set `ANTHROPIC_API_KEY` (environment variable or Streamlit secrets; see `.streamlit/secrets.toml.example`). The model name is configurable through `CLAIM_LLM_MODEL`.

## Deploy (Streamlit Community Cloud)

1. Push this repository to GitHub (`app.py` and `requirements.txt` at the root).
2. On [share.streamlit.io](https://share.streamlit.io), choose **Create app**, then select the repository, branch `main` and main file `app.py`.
3. Optional: under **App settings, Secrets**, add `ANTHROPIC_API_KEY = "..."`.
4. Open the live URL in a private window, go to **Test cases** and click **Run all four**.

Free hosting can reset local storage on restart. For that reason every test run is downloadable as JSON, and sample evidence is committed in [`/evidence`](evidence/).

## Official test cases

Each case runs with one click on the **Test cases** page. Latest results are in [`evidence/SUMMARY.md`](evidence/SUMMARY.md).

| Case | Scenario | What the run proves |
|---|---|---|
| **TC1** | Missing invoice requested | Status `DOCS_MISSING` read from the portal; proposal for the right file and claim (a file for another claim is ignored); no upload before approval; approval stored with file hash and destination; upload; verifier confirms the file is present once |
| **TC2** | Another family member's claim | `CONSENT_REQUIRED`; ticking a checkbox alone is not consent; zero portal actions; upload proposal blocked; proceeds only after the holder's recorded consent |
| **TC3** | "Under review" | Mapped to `UNDER_REVIEW`; no approval wording anywhere; an injected "your claim is approved" message is rejected |
| **TC4** | Upload times out | **A:** timeout after the file landed, so the list is re-read and the result is `SKIPPED_DUPLICATE` (the portal saw one upload). **B:** timeout before it landed, so the list is re-read, one retry runs with the same idempotency key, and the verifier confirms exactly one copy |

## Safety guarantees

All of these are enforced in code and covered by tests.

- Status comes from the portal. Unrecognised values map to `UNKNOWN` ("manual verification required"). Matching is exact, so "not approved" never becomes approved.
- Not the policy holder and no recorded consent: the agent stops before any portal action.
- No upload without a stored approval record that shows the file, destination claim and exact change.
- Idempotency key is `claim_no:file_sha256`. On a timeout the document list is re-read first, and there is at most one retry.
- Verification is a separate class that shares no code with the executor.
- Failed and pending results stay visible and are never shown as success.

## Evidence map

| Requirement | Proof | Where |
|---|---|---|
| Reads the actual status | Portal adapter and `map_status` | TC1/TC3 evidence (raw status, source ID, timestamp) |
| Requires consent | `Registry.check_authorization` runs before any portal call | TC2, `test_blocked_request_causes_zero_portal_actions` |
| Approval before upload | Approval row stored before the write | TC1 approval records, `test_no_upload_without_approval_record` |
| No duplicate upload | Idempotency key and list re-read | TC4 logs, `test_timeout_*`, `test_double_click_never_double_uploads` |
| Verifies the upload | Verifier (before and after lists, status stamp) | TC1/TC4 verification, `test_verifier_detects_duplicates_and_missing` |
| Never "approved" while under review | Exact status map and `validate_message` | TC3, `test_validate_message_*` |
| Works when the LLM fails | Template fallback | `test_template_fallback_runs_with_llm_disabled`, `test_llm_failure_falls_back_without_breaking` |

## Project structure

| File | Purpose |
|---|---|
| `contract.py` | Frozen enums, models and the five contract functions |
| `portal.py` | `PortalAdapter` and `MockPortal` (state in SQLite, injectable timeouts) |
| `engine.py` | Status map, consent registry, document mapper, idempotency key, message validator, `Verifier` |
| `service.py` | The contract functions; the only code path that writes to the portal is `execute_upload` |
| `llm.py` | All LLM calls: request parsing, tool loop, message phrasing, fallback |
| `db.py` | SQLite storage: runs, proposals, approvals, executions, verifications, test results, consents |
| `testcases.py` | TC1-TC4 runners and evidence export |
| `app.py` | Streamlit UI |
| `fixtures/` | Synthetic claims, registry and placeholder PDFs |
| `tests/` | 39 unit and integration tests |
| `evidence/` | Sample evidence from real test runs |

## Portal integration

The agent talks to the insurer portal only through `PortalAdapter` (`get_claim`, `list_documents`, `upload_document`). The repository ships with `MockPortal`, a synthetic insurer portal. To target a different portal, implement the adapter in `portal.py` and pass it to `ClaimService(portal=...)`. If a portal words its statuses differently, extend `_STATUS_MAP` in `engine.py`; unmapped values are shown safely as unknown.

## Scope and limitations

- Results in this repository come from the synthetic mock portal. Behaviour of any other portal (fields, timeouts, access method) is not covered by these tests.
- Consent is simulated with a mock control; no message is sent to a real policy holder.
- Identity is not verified: entering the policy holder's exact name is treated as being the holder. A production system would use the insurer's own login or OTP, which this project deliberately does not simulate or bypass.
- Timeouts are injected by the mock portal for testing.
- The optional AI path is tested with scripted model responses (guards, tool restrictions, fallback) and by manual use, not with an automated live-API test suite.
- A single demo database is shared by all visitors of one deployment.
- The PDF files are small synthetic placeholders.