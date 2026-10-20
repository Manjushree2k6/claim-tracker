# Insurance Claim Status Tracker - Agentic AI Hackathon (Healthcare & Wellness AI)

A family member asks *"Where is my father's claim?"* and gets a clear **status**, a **missing-document list** and a
**next step**. With explicit human approval, the agent uploads the right approved document to the right claim, then an
independent verifier re-reads the portal to prove it.

> Demo target: our synthetic mock insurer portal behind a `PortalAdapter` interface (the organiser's mock portal plugs
> in at the same place - see "Organiser portal"). **Synthetic data only.**

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py          # open the URL it prints
python run_tests.py           # or: pytest -q      -> 39 tests
python make_evidence.py       # regenerate /evidence from fresh test runs
```

No API key is needed: without one the agent runs its deterministic template flow and all four test cases pass.
To enable the optional AI assistant, set `ANTHROPIC_API_KEY` (env var or Streamlit secrets - see
`.streamlit/secrets.toml.example`). Model name is configurable via `CLAIM_LLM_MODEL`.

## Deploy (Streamlit Community Cloud)

1. Push this folder to a GitHub repo (root contains `app.py`, `requirements.txt`).
2. share.streamlit.io -> *New app* -> pick the repo, branch, main file `app.py`.
3. (Optional) App settings -> Secrets -> paste `ANTHROPIC_API_KEY = "..."`.
4. Open the live URL in a **fresh/incognito browser**, press *Test cases -> Run all four*.

Free hosting can reset local storage on restart. That is why every test run is downloadable as JSON and sample
evidence is committed in `/evidence`.

## Agent loop

`PLAN -> TOOL CALL -> OBSERVE -> DECIDE -> APPROVE -> ACT -> VERIFY` (shown with timestamps in the *Agent trace* panel).

| Who | Does |
|---|---|
| **LLM (optional)** | parses the question, picks the next tool, suggests a file, words the final message |
| **Code (always)** | validates input, maps status, checks consent, computes missing docs, matches files, enforces approval, uploads, prevents duplicates, verifies |

The LLM can only call `get_status`, `list_documents`, `check_consent`, `propose_upload`. `execute_upload` is not exposed
to it. Identity (claim / policy / requester) always comes from the validated form, never from model output. Any LLM
message that uses approval-like wording for a non-approved status, or adds IDs / numbers not in the tool results, is
discarded and replaced by a template. If the API fails, the template flow runs.

## Repo map

| File | Purpose |
|---|---|
| `contract.py` | frozen enums, models and the five contract functions |
| `portal.py` | `PortalAdapter` + `MockPortal` (state in SQLite, injectable timeouts) |
| `engine.py` | status map, consent registry, document mapper, idempotency key, message validator, **Verifier** |
| `service.py` | the contract functions; the only code path that writes to the portal is `execute_upload` |
| `llm.py` | all LLM calls: `parse_request`, tool loop, `phrase_result`, fallback |
| `db.py` | SQLite: runs, proposals, approvals, executions, verifications, test results, consents |
| `testcases.py` | TC1-TC4 runners + evidence export |
| `app.py` | Streamlit UI |
| `fixtures/` | synthetic claims, registry, PDFs (`python fixtures/make_fixtures.py` regenerates them) |
| `tests/` | 39 unit/integration tests |
| `evidence/` | sample evidence from real test runs (`SUMMARY.md` + one JSON per case) |

*(Added beyond the original repo plan: `service.py` (wiring + executor), `testcases.py`, `make_evidence.py`, `run_tests.py`.)*

## Official test cases

Run each with one click on the **Test cases** page. Latest results are in `evidence/SUMMARY.md`.

| TC | Scenario | What the run proves |
|---|---|---|
| TC1 | Missing invoice requested | status `DOCS_MISSING` read from portal -> proposal for the right file/claim (a file for another claim is ignored) -> no upload before approval -> approval stored with file hash + destination -> uploaded -> verifier `VERIFIED` (file present once) |
| TC2 | Another family member's claim | `CONSENT_REQUIRED`; ticking the checkbox alone is not consent; **zero portal actions**; upload proposal blocked; after the holder's (simulated) consent it can proceed |
| TC3 | "Under review" | mapped to `UNDER_REVIEW`; no approval wording anywhere; a rogue "your claim is approved" AI message is rejected |
| TC4 | Upload times out | A: timeout after the file landed -> list re-read -> `SKIPPED_DUPLICATE`, portal saw 1 upload call. B: timeout before it landed -> re-read -> retried once with the same idempotency key -> verifier confirms exactly one copy |

## Safety rules (enforced in code, covered by tests)

1. Status comes from the portal; unrecognised status -> `UNKNOWN` ("manual verification required"). Matching is exact, so "not approved" never becomes approved.
2. Not the policy holder + no recorded consent -> stop before any portal action.
3. No upload without a stored approval record (file, destination claim, exact change shown).
4. Idempotency key = `claim_no:file_sha256`; on timeout re-read first; retry at most once.
5. Verification is a separate class that shares no code with the executor.
6. Failed / pending results stay visible. Synthetic data only. OTP / CAPTCHA are never bypassed.

## Evidence map

| Requirement | Proof | Where |
|---|---|---|
| Reads actual status | portal adapter + `map_status` | TC1/TC3 evidence (`raw_status`, source id, timestamp) |
| Requires consent | `Registry.check_authorization` runs before any portal call | TC2 + `test_blocked_request_causes_zero_portal_actions` |
| Approval before upload | approval row stored before the write | TC1 `approval_records`, `test_no_upload_without_approval_record` |
| No duplicate upload | idempotency key + list re-read | TC4 logs, `test_timeout_*`, `test_double_click_never_double_uploads` |
| Verifies upload | `Verifier` (before/after lists + stamp) | TC1/TC4 verification, `test_verifier_detects_duplicates_and_missing` |
| Never "approved" while under review | exact status map + `validate_message` | TC3, `test_validate_message_*` |
| Works when the LLM fails | template fallback | `test_template_fallback_runs_with_llm_disabled`, `test_llm_failure_falls_back_without_breaking` |

## Organiser portal (to do on 20 Oct)

Implement `get_claim`, `list_documents`, `upload_document` of `PortalAdapter` in `portal.py` and pass that object to
`ClaimService(portal=...)`. **Do not assume an API:** first inspect what the portal exposes; if it is web-only use
Playwright inside the adapter; never bypass OTP/CAPTCHA; ask the organiser how a timeout can be demonstrated. Assumptions
currently baked into the mock are listed at the top of `portal.py` (status text values, `required_docs`, document
`doc_type`/`sha256`, status stamp, status moving to "Under Review" when documents complete). If the real portal words
statuses differently, extend `_STATUS_MAP` in `engine.py` - unmapped values safely show as *unknown*.

## Honest limitations

- Only tested against the **mock** portal; organiser-portal behaviour (fields, timeouts, access method) is unverified.
- Consent is a mock button; nothing is sent to a real policy holder.
- Identity is not verified: typing the policy holder's exact name counts as being the holder (no login/OTP; we never bypass one).
- Timeouts are injected by the mock portal.
- The optional AI path was tested with scripted fake models (guards, tool restrictions, fallback), not a live API call.
- The UI was exercised with a stand-in for Streamlit's API in development, so first-run on real Streamlit should be checked.
- Single shared demo database: all visitors of one deployment share runs and approvals.
- Local "PDF" files are tiny synthetic placeholders.
