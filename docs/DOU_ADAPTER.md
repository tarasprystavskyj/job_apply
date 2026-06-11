# DOU / Relocate DOU Adapter Slice

Created: 2026-06-11

This slice is discovery, normalization, scoring, local drafting, review, manual
handoff, and status tracking only. It does not log in, inspect cookies, read
browser profiles, read private resume text, upload files, click apply controls,
send messages, or change DOU account state.

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

## Next Integration Steps

- Surface DOU jobs and drafts from the shared DB in the web review queue.
- Add owner approval UI fields for exact message/resume/final action.
- Keep DOU final application as manual handoff until a separate prepare-only
  browser adapter is designed, reviewed, and explicitly approved.
