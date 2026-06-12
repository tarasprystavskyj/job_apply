# DOU / Relocate DOU Adapter Slice

Created: 2026-06-11

This adapter slice is discovery, normalization, scoring, local drafting, review,
manual handoff, and status tracking only. It does not log in, inspect cookies,
read browser profiles, read private resume text, upload files, click apply
controls, send messages, or change DOU account state.

A separate guarded live submitter exists at `src/dou_csv_apply.py`. It attaches
to an already-running Chrome CDP session and only processes rows that carry all
row-level approval gates. The registry adapter remains review-only so discovery
and shared DB flows cannot submit by accident.

## Safe Scope

- Adapter: `src/job_platforms/dou.py`
- Run-once helper: `src/dou_pipeline.py`
- Registry wiring: `default_registry().get("dou")`
- Public hosts: `jobs.dou.ua`, `relocate.dou.ua`, and `dou.ua`
- Shared state: `data/job_waves/job_apply_shared.sqlite3`

The adapter parses saved public DOU listing/detail HTML and JSON-LD into
`job.vacancy_observation.v0` records. The shared DB helpers upsert public jobs,
store local review-only outreach drafts, and append status/events for UI
progress.

## Pipeline Gates

1. `discover`
   - Build public listing URLs only:
     - `https://jobs.dou.ua/vacancies/?search=<query>`
     - `https://relocate.dou.ua/jobs/?search=<query>`

2. `normalize`
   - Extract public vacancy title, company, location, summary, salary/freshness
     hints, fit tags, and risk flags.
   - Output shape: `job.vacancy_observation.v0`.

3. `score`
   - Score public text locally with DOU-specific fit weights.
   - No `.env`, resume index, browser profile, or account data is read.

4. `draft`
   - Generate local `job.application_draft.v0` messages through the shared base
     adapter.
   - Resume handle is metadata-only:
     `owner_selected_resume_metadata_pending`.

5. `review_approval`
   - Draft rows stay `approval_status=needs_owner_review`.
   - `submission_allowed=false` and `upload_allowed=false`.

6. `final_gated_apply`
   - Current state is manual handoff.
   - `prepare_application` and `final_submit` fail closed behind the base safety
     gates and raise `UnsupportedAction` after approval.
   - Live browser sending is only available through `src/dou_csv_apply.py`,
     with explicit CSV gates and CLI flags.

7. `status_tracking`
   - Shared DB events and statuses provide graph-friendly progress data.
   - `build_dou_progress_snapshot()` emits
     `job.progress_snapshot.langgraph.v0`.

## Progress Graph

The DOU progress graph contains gate nodes for:

- `discover`
- `normalize`
- `score`
- `draft`
- `review_approval`
- `final_gated_apply`
- `status_tracking`

Each node includes safe UI metadata:

- `platform=dou`
- `order`
- `description`
- `safe_actions_only=true`
- `submission_automation_enabled=false`

Observed jobs and outreach drafts are added as child nodes with public URL,
score, tags, approval flags, and manual handoff status. Private CV text and
account state are not included.

## Run-Once Public Discovery

The bounded helper fetches public DOU/Relocate DOU pages, normalizes vacancies,
stores review-only drafts in the shared DB, and writes approval/blocker
artifacts. It never opens an application form, uploads a resume, sends a
message, changes account state, or reads `.env`, cookies, saved browser
profiles, or private resume text.

Default command:

```powershell
python src\dou_pipeline.py --query "Python AI" --limit 10 --max-pages 2
```

Dry-run command, with no shared DB draft/status writes:

```powershell
python src\dou_pipeline.py --query "Python AI" --limit 3 --max-pages 1 --dry-run
```

Artifacts:

- `data/job_waves/dou_observations.jsonl`
- `data/job_waves/dou_progress_snapshot.json`
- `data/job_waves/dou_run_once_summary.json`
- `data/job_waves/dou_blockers.json`

The blocker artifact records that final submit is not implemented and that exact
owner approval is still required per vacancy, message, resume decision, and any
future final submit action.

## Guarded Live Submitter

`src/dou_csv_apply.py` supports three safe modes:

```powershell
python src\dou_csv_apply.py --csv path\to\approved_dou.csv
```

Validation-only. This reads CSV gate metadata and writes
`data/job_waves/dou_submission_attempts.jsonl`; it does not open a browser or
type personal data.

```powershell
python src\dou_csv_apply.py --csv path\to\approved_dou.csv --prepare --i-understand-this-fills-dou-form
```

Pre-submit browser preparation. It opens the approved DOU vacancy URL in Chrome
through CDP, opens the visible application surface, fills the exact approved
message and approved LinkedIn policy fields when present, validates the visible
form, logs the result, and stops before final submit.

```powershell
python src\dou_csv_apply.py --csv path\to\approved_dou.csv --execute --i-understand-this-sends-dou-applications
```

Final send. This is refused unless the row and CLI gates are present.

Required CSV columns:

- `site=dou`
- `url`: exact ID-specific `https://jobs.dou.ua/.../vacancies/<id>/` or
  `https://relocate.dou.ua/.../vacancies/<id>/` URL
- `message`: exact approved application message text
- `linkedin_policy`: `omit`, `fill_url`, or `use_site_profile`
- `linkedin`: required only when `linkedin_policy=fill_url`
- `resume_policy`: `no_resume`, `use_site_profile_resume`, or `upload_file`
- `upload_allowed`: must be `false` unless `resume_policy=upload_file`
- `approved_resume_name`: required only for `resume_policy=upload_file`
- `approved_to_submit=true`
- `final_submit_allowed=true`

Resume upload is intentionally blocked in this implementation. Rows with
`resume_policy=upload_file` must name the exact approved resume and
`upload_allowed=true`, but the script still returns a validation blocker because
no safe uploader has been implemented and no resume files are read.

Public smoke without typing or submitting can inspect a broader public
DOU vacancy/listing URL:

```powershell
python src\dou_csv_apply.py --public-smoke-url https://jobs.dou.ua/companies/example/vacancies/123/
```

## Next Integration Steps

- Surface DOU jobs and drafts from the shared DB in the web review queue.
- Add owner approval UI fields for exact message/resume/final action.
- Keep DOU final application as manual handoff until a separate prepare-only
  browser adapter is designed, reviewed, and explicitly approved.
