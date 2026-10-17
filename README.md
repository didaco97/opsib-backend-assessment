# OPSiB Backend Engineering Assessment

Retail data integration, exception detection and task reliability: a small
FastAPI + PostgreSQL service that imports ERP/WMS/HRMS extracts, reconciles
them into a canonical model, detects receipt shortfalls, routes investigation
tasks to eligible employees and tracks work through an audited lifecycle.

## Quick start

Requirements: Python 3.10+, and either PostgreSQL (recommended) or nothing
(SQLite fallback for a zero-setup run).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Option A: PostgreSQL via docker compose (credentials come from .env / your shell)
cp .env.example .env   # then set POSTGRES_PASSWORD and DATABASE_URL to match
set -a; source .env; set +a
docker compose up -d db

# Option B: zero-setup SQLite (limitations noted below)
export DATABASE_URL=sqlite:///./opsib.db

uvicorn app.api.main:app --reload
```

Tables are created on startup. Interactive API docs: http://localhost:8000/docs

Load the supplied fixture pack and run detection:

```bash
curl -X POST localhost:8000/imports/fixture-pack
curl -X POST localhost:8000/detection/run
curl localhost:8000/exceptions
curl "localhost:8000/tasks?assignee_id=HR-102"
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./opsib.db` | SQLAlchemy URL. PostgreSQL for a real pilot; set credentials via env, never in source. |
| `EVAL_TIME` | `2026-10-16T15:00:00+05:30` | **Frozen scenario clock.** All deadline and shift logic uses this, never the machine clock. |
| `FIXTURES_DIR` | `fixtures` | Where `POST /imports/fixture-pack` reads the CSVs. |
| `AVAILABILITY_FRESHNESS_MINUTES` | `60` | Max age of a workforce sample for routing eligibility at `EVAL_TIME`. |
| `TASK_DUE_HOURS` | `24` | Due time assigned to new investigation tasks. |
| `SHORTFALL_TOLERANCE` | `0` | Variance tolerance in base UOM. Zero per the brief; configurable to prove the knob. |

## Running the tests

```bash
pytest                 # zero-setup: in-memory SQLite
TEST_DATABASE_URL=postgresql://opsib:$POSTGRES_PASSWORD@localhost:5432/opsib_test pytest
```

Verified locally: **33 passed** on both in-memory SQLite and PostgreSQL 14.
Coverage of the required minimum:

- `tests/test_uom.py` — UOM conversion, factor identity, mismatch/missing-alias rejection
- `tests/test_detection_unit.py` — partial receipts, deadline boundary (exactly at `EVAL_TIME` is actionable), cancelled lines, unposted receipts, tolerance, neutral-cause wording
- `tests/test_integration_scenarios.py` — full fixture pack; asserts all six required scenario outcomes
- `tests/test_idempotency.py` — double import of identical files changes nothing; detection re-run creates no new exceptions or tasks
- `tests/test_routing.py` — location mapping, off-shift exclusion (HR-101), inactive exclusion (HR-103), fallback manager (HR-401), explicit-assignee validation
- `tests/test_workflows.py` — invalid transitions rejected (409), repeated completion is a safe replay, reassign validation + audit, verification gating, audit order, reopen path
- `tests/test_failure_paths.py` — unknown product mapping (data-quality, not exception), conflicting duplicate source ID flagged not overwritten, invalid rows visible with reasons, no-eligible-assignee stays unassigned

## Architecture

```
app/
  api/         FastAPI surface (imports, data-quality, detection, exceptions, tasks)
  ingestion/   CSV parsing, typed row validation (pydantic), idempotent upsert
  mapping/     canonical product/location resolution + UOM conversion
  detection/   deterministic receipt-shortfall rule (receipt-shortfall/v1)
  routing/     employee eligibility and assignment policy (routing/v1)
  workflows/   server-enforced task state machine and lifecycle operations
  ai/          optional investigation-summary provider boundary (schema-checked)
  db/          SQLAlchemy models; AwareDateTime keeps every timestamp tz-aware
fixtures/      the supplied data pack, unchanged
docs/openapi.json  exported API contract
```

Source-specific parsing lives in `app/ingestion/`; the detector in
`app/detection/` reads only canonical, already-mapped rows. Adding another
retailer's export means adding a parser/mapper, not touching the rule engine.

### Data model

- `import_runs` / `import_row_errors` — every file load with accepted /
  rejected / duplicate / conflict counts and per-row rejection reasons.
- `product_aliases`, `location_aliases` — versioned crosswalks
  (`mapping/v1`). Identity comes only from these tables, never name matching.
- `po_lines`, `receipt_lines` — source facts with full lineage
  (source_record_id as primary key, import reference, content hash) plus
  derived canonical fields (`canonical_product_id`, `canonical_location_id`,
  `*_qty_base`). NULL canonical = unresolved mapping, surfaced by
  `/data-quality`.
- `employees`, `workforce_status` — HRMS roster (authoritative for employment
  status) and the latest availability sample per employee.

Which system is authoritative: the ERP export owns PO identity, quantities and
deadlines; the WMS export owns received quantities and receipt timing; the
HRMS roster owns employment status, role and org scope; the workforce export
owns shift availability but never overrides HRMS employment status. Raw source
rows are immutable once accepted — canonical fields are derived alongside them
in the same tables (a deliberate simplification for this slice), and a changed
source record becomes a conflict for review rather than an edit.
- `source_conflicts` — same source ID arriving with different content. The
  existing row is never overwritten; the conflict waits for review.
- `exceptions` / `exception_events` — business cases with a stable,
  idempotent `case_key`, evidence references and audit history.
- `tasks` / `task_events` — the work, separate from the case; reassignment
  and every state change are audited.

## Design answers

**Repeat/concurrent imports.** Each source record is keyed by its
`source_record_id` primary key. Identical repeats (compared by content hash)
are counted as duplicates and skipped; the same ID with different content
becomes a `SourceConflict` instead of an overwrite. Detection derives its
idempotency from a unique `case_key` = SHA-256(tenant, rule version, PO,
line): a second run or a second worker finds the existing exception and
creates nothing. Task creation checks for an existing task per exception
inside the same transaction, so two workers cannot spawn two investigations
for one business issue.

**Client timeout after a committed completion.** State changes lock the task
row (`SELECT ... FOR UPDATE` on PostgreSQL) and check the current status. If
`complete` committed but the client never saw the response, the retry finds
the task already in `COMPLETED` (or later) and returns the current state with
`idempotent_replay: true` — no 409, no duplicate audit event.

**Why a shortfall, not supplier fault.** The fixture pack has no dispatch/ASN
data. Findings carry a neutral explanation and an explicit `missing_evidence`
list; nothing in the code path can assert cause.

**Receipt-grain correctness.** Aggregation is over deduplicated, POSTED
receipt lines linked by `(po_number, po_line_number)`, each converted to base
UOM at import time. PO-506's repeated source record is inserted once
(primary key), so it is counted once: shortfall 10 EA, not 0 EA.

## Required scenario outcomes (asserted by `tests/test_integration_scenarios.py`)

| PO | Result |
|---|---|
| PO-501 | Shortfall **8 EA** (60 + 32 posted of 100) |
| PO-502 | No exception (40 of 40) |
| PO-503 | Skipped: deadline in the future at `EVAL_TIME` |
| PO-504 | Skipped: PO `CANCELLED` |
| PO-505 | Shortfall **24 EA** (10 BOX x 12 = 120 EA expected, 96 EA posted) |
| PO-506 | Shortfall **10 EA** (duplicate source record counted once) |

Routing at the frozen time: STORE-014 cases go to **HR-102** (HR-101 is
OFF_SHIFT; HR-103 is INACTIVE even though the shift export says ON_SHIFT —
employment status wins). STORE-015 has no receiving associate, so the task
falls back to store manager **HR-401**.

## API summary

Full contract: `docs/openapi.json` or `/docs` on a running server.

- `POST /imports/fixture-pack` — load all six CSVs in dependency order
- `POST /imports/{dataset}` — upload one CSV (multipart field `file`)
- `GET /imports/{import_id}` — counts, row errors, status
- `GET /data-quality` — unresolved mappings, rejected-record visibility, open source conflicts, employees without store scope
- `POST /detection/run` — run the shortfall rule; returns created/existing/skipped and the tasks created
- `GET /exceptions`, `GET /exceptions/{id}` — findings with evidence and audit
- `GET /tasks?assignee_id=&location=&status=` — work lists
- `POST /tasks/{id}/acknowledge | start | complete | request-verification | verify | reject | reopen | reassign`
- `POST /exceptions/{id}/investigation-summary` — optional AI boundary (503 when no provider is configured)

Lifecycle: `CREATED -> ASSIGNED -> ACKNOWLEDGED -> IN_PROGRESS -> COMPLETED
-> PENDING_VERIFICATION -> VERIFIED`. `REJECTED` (from `PENDING_VERIFICATION`,
reason required) records failed verification; `REOPENED` returns work from
`VERIFIED`/`REJECTED`/`PENDING_VERIFICATION` to `IN_PROGRESS`; `BLOCKED` can
be entered from the active states. Anything else is refused server-side (409).
`REJECTED` leaves the exception `OPEN` — only verified completion resolves it. An exception becomes `RESOLVED` only when its task is
verified with a resolution code and evidence reference.

## AI boundary (Task D)

`app/ai/provider.py` defines a `SummaryProvider` protocol: it receives a
typed case-context package (facts + evidence record IDs only) and must return
schema-validated JSON whose cited record IDs all exist in the supplied
evidence. Invalid or unverifiable output is rejected (502) and never touches
business state. No live key is needed or expected; the deterministic detector
remains the sole authority for quantities, eligibility and status.

## Assumptions and trade-offs

- **Actionable at the deadline, not after it.** "Deadline has passed at the
  frozen evaluation time" is implemented as `expected_receipt_by <= EVAL_TIME`.
- **Availability freshness.** A shift sample is usable for 60 minutes before
  `EVAL_TIME` (configurable). The fixture samples (observed 10 minutes before
  the frozen time) are fresh.
- **Latest availability sample wins.** A newer `observed_at` supersedes; an
  older or equal-but-different sample becomes a source conflict.
- **One investigation task per exception.** The model allows several tasks
  per exception (reassignment adds no new exception); the pilot creates one.
- **SQLite fallback** is for zero-setup evaluation only: it lacks true
  `SELECT ... FOR UPDATE` and timestamptz, and its concurrency story is
  weaker. For a live pilot, run PostgreSQL (compose file included); the row
  lock and unique constraints are what make the retry/concurrency answers
  true under parallel workers.
- **Logging/errors:** API errors carry machine-readable detail strings;
  lifecycle events log structured key=value lines (IDs, counts, states) with
  no credentials, payloads or personal data.
- **Not built (documented, not claimed):** tenant-level authn/authz, secret
  management, migrations (tables are created from metadata; Alembic would be
  the production path), monitoring, retention, and the live Claude client
  behind the AI provider protocol.
- **Fixture files are used unchanged;** derived test data in
  `tests/test_failure_paths.py` is created inline, never by editing fixtures.

## AI tooling disclosure

This implementation was generated with AI coding assistance: an AI agent
wrote the code, tests and documentation against the assessment brief and
fixture pack, and iterated until the full suite passed on PostgreSQL 14 and
SQLite. I (the candidate) take responsibility for every submitted line: I
reviewed the design decisions, the rule semantics, the routing policy and the
test assertions, and can explain and modify the implementation in the
walkthrough.
