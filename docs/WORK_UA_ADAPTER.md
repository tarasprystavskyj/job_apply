# Work.ua Adapter Slice

Created: 2026-06-11

This slice includes public discovery, normalization, scoring, drafting, review,
manual handoff, status tracking, and a separately gated browser-assisted CSV
adapter for Work.ua prepare/submit attempts. It does not log in, inspect
cookies, read browser profiles, upload resumes, or change Work.ua account
settings.

## Files

- `src/job_platforms/workua.py`: Work.ua adapter, public HTML extractor, shared
  DB helpers, run-once public search helper, artifact writer, and graph progress
  snapshot.
- `src/workua_public_run_once.py`: bounded CLI wrapper for public search URL
  fetch/read and review-only artifact generation.
- `src/workua_csv_apply.py`: browser-assisted CSV adapter for explicitly
  approved Work.ua rows. Dry-run is default; browser prepare and final submit
  require separate CLI flags.
- `src/workua_resume_update.py`: generic guarded Work.ua resume edit helper.
  It accepts exact resume edit URLs and exact approved field updates, can fill
  them through Chrome CDP, and stops before final save unless separately gated.
- `src/job_platforms/platforms.py`: registry-facing import for `WorkUaAdapter`.
- `tests/test_workua_adapter.py`: safe parser, gate, shared DB, and progress
  coverage.
- `tests/test_workua_csv_apply.py`: CSV gate, upload block, LinkedIn policy, and
  dry-run attempt-log coverage.
- `tests/test_workua_resume_update.py`: resume edit URL validation, approval
  gates, no-save default, and generic non-hardcoded config coverage.

## Pipeline Gates

1. `discover`
   - Build public search/listing URLs with `WorkUaAdapter.discovery_urls`.
   - Example output: `https://www.work.ua/jobs-Python+AI/`.
   - `fetch_public_workua_url` fetches only HTTP(S) pages on `work.ua` /
     `www.work.ua`, sends no cookies, and caps response size.

2. `normalize`
   - Convert caller-provided public listing/detail HTML into
     `job.vacancy_observation.v0`.
   - Extractors support `JobPosting` JSON-LD and public `/jobs/<id>/` anchors.

3. `score`
   - Score with the shared `vacancy_pipeline.score_vacancy` shape plus a small
     Work.ua-specific remote/manual-handoff adjustment.
   - Persist into the shared `jobs` table with `platform=workua`.

4. `draft`
   - Generate local `job.application_draft.v0` text through the shared adapter
     base.
   - Drafts always keep `submission_allowed=false` and `upload_allowed=false`.
   - Resume is represented as metadata only:
     `owner_selected_resume_metadata_pending`.

5. `review_approval`
   - Owner review is required before any concrete action.
   - Approval must name the exact vacancy, resume/profile, message text, and
     final action policy.

6. `final_gated_apply_manual_handoff`
   - Review artifacts still stop at manual owner review.
   - Executable Work.ua browser work is handled only by
     `src/workua_csv_apply.py`.
   - Browser prepare requires row-level `approved_to_submit=true`,
     `final_submit_allowed=true`, an exact message, resume/LinkedIn policy, and
     `--prepare-browser --i-understand-this-prepares-workua-application`.
   - Final submit additionally requires
     `--execute --i-understand-this-submits-workua-application`.
   - Resume file upload is intentionally unsupported. A row using
     `resume_policy=upload_resume` must name the exact resume and set
     `upload_allowed=true`, but the current adapter still blocks before upload.

7. `status_tracking`
   - `persist_workua_observation` and `create_workua_review_draft` write to the
     shared SQLite DB and append events.
   - `build_workua_progress_snapshot` returns graph data for UI consumers.

## Run Once CLI

Safe public discovery smoke:

```powershell
python src\workua_public_run_once.py --query "Python AI" --limit 3
```

Optional explicit public search/listing URL:

```powershell
python src\workua_public_run_once.py --query "Python AI" --source-url "https://www.work.ua/jobs-Python+AI/" --limit 3
```

The CLI:

- fetches public Work.ua HTML only;
- extracts vacancies from `JobPosting` JSON-LD and public `/jobs/<id>/` links;
- persists normalized rows into the shared SQLite DB;
- creates local review drafts with `submission_allowed=false` and
  `upload_allowed=false`;
- writes approval artifacts and blocker artifacts under
  `data/job_waves/workua_artifacts/` by default;
- stops before any application form action.

It has no execute/apply/send/upload/profile-save flag.

## Live CSV Adapter

Dry-run validation:

```powershell
python src\workua_csv_apply.py --csv examples\workua_approved_jobs_sample.csv
```

Browser pre-submit prepare, with no final click:

```powershell
python src\workua_csv_apply.py --csv path\to\approved_workua.csv --prepare-browser --i-understand-this-prepares-workua-application
```

Final submit:

```powershell
python src\workua_csv_apply.py --csv path\to\approved_workua.csv --execute --i-understand-this-prepares-workua-application --i-understand-this-submits-workua-application
```

Required CSV columns:

- `site`: must be `workua`
- `url`: `https://www.work.ua/jobs/<id>/`
- `message`: exact approved application message
- `resume_policy`: `no_upload`, `no_resume`, `use_workua_profile`,
  `use_selected_resume`, or `upload_resume`
- `linkedin_policy`: `no_linkedin`, `include_in_message`, or `fill_field`
- `upload_allowed`: `true` only for `resume_policy=upload_resume`
- `approved_to_submit`: must be `true` for browser prepare/execute
- `final_submit_allowed`: must be `true` for browser prepare/execute

Optional CSV columns:

- `title`
- `company`
- `linkedin`: required for `include_in_message` or `fill_field`
- `approved_resume_name`: required for `use_selected_resume` and
  `upload_resume`

The adapter opens the public Work.ua vacancy in an already-running Chrome CDP
session, navigates only through visible apply links, fills the exact message and
LinkedIn field when policy says to do so, validates the DOM immediately before
submit, logs a JSONL attempt under `data/job_waves/`, and stops before final
submit unless `--execute` and the final submit confirmation flag are present.
The attempt log stores message length and SHA-256, not the message body.

## Resume Update Helper

`src/workua_resume_update.py` is a separate generic tool for Work.ua resume edit
pages such as:

```text
https://www.work.ua/jobseeker/my/resumes/edit/?id=3508069
```

It is not hard-coded to one owner or one resume id. The input must provide:

- `site=workua`
- `resume_edit_url`: exact Work.ua resume edit URL with an `id` query
- `field_updates`: exact approved field updates
- `approved_to_prepare=true` for browser fill/prepare
- `final_save_allowed=true` for final save

Each field update must use exactly one locator: `selector`, `name`, or `label`.
Supported field kinds are `text`, `textarea`, `select`, `checkbox`, and
`radio`. Values are logged only as length and SHA-256 digest.

Dry-run validation, no browser:

```powershell
python src\workua_resume_update.py --json path\to\workua_resume_update.json
```

Browser prepare, no save:

```powershell
python src\workua_resume_update.py --json path\to\workua_resume_update.json --prepare-browser --i-understand-this-prepares-workua-resume-update
```

Final save:

```powershell
python src\workua_resume_update.py --json path\to\workua_resume_update.json --execute-save --i-understand-this-prepares-workua-resume-update --i-understand-this-saves-workua-resume-update
```

Minimal JSON shape:

```json
{
  "site": "workua",
  "resume_edit_url": "https://www.work.ua/jobseeker/my/resumes/edit/?id=123",
  "field_updates": [
    {
      "key": "title",
      "selector": "#resume-title",
      "kind": "text",
      "value": "Senior Python Engineer"
    }
  ],
  "approved_to_prepare": true,
  "final_save_allowed": false,
  "resume_file_policy": "no_upload"
}
```

Optional resume file upload is gated by `resume_file_policy=upload_exact_file`,
`upload_allowed=true`, exact `approved_resume_name`, exact
`approved_resume_path`, and the same prepare/save CLI flags. Tests do not read
private resume contents.

## Artifacts

Approval artifact schema: `job.workua_approval_artifact.v0`

Important fields:

- `job_id`
- `outreach_id`
- `source_url`
- `title`
- `company`
- `score`
- `fit_tags`
- `risk_flags`
- `draft_text`
- `approval_required=true`
- `submission_allowed=false`
- `upload_allowed=false`
- `required_owner_approval`

Blocker artifact schema: `job.workua_blocker_artifact.v0`

Blockers are written when public fetch/parse fails or no public vacancies are
extracted. They also keep `submission_allowed=false` and `upload_allowed=false`.

Summary artifact schema: `job.workua_run_once_summary.v0`

The summary includes persisted job ids, approval artifact paths, blocker
artifact paths, and the Work.ua progress snapshot.

## Progress Snapshot

Schema: `job.workua_progress_snapshot.langgraph.v0`

Node ids are prefixed with `workua:`:

- `workua:discover`
- `workua:normalize`
- `workua:score`
- `workua:draft`
- `workua:review_approval`
- `workua:final_gated_apply_manual_handoff`
- `workua:status_tracking`
- `workua:job:<id>`
- `workua:outreach:<id>`

Important node data fields:

- `platform`
- `order`
- `description`
- `owner_approval_required`
- `source_url`
- `score`
- `fit_tags`
- `risk_flags`
- `submission_allowed`
- `upload_allowed`
- `approved_to_submit`
- `final_submit_allowed`

Expected statuses include `pending`, `ready`, `seen`, `complete`,
`blocked_waiting_owner`, `active`, and `manual_handoff_ready`.

## Safe Integration Steps

1. Add a low-rate public fetcher only after query/rate limits are approved.
2. Surface Work.ua rows from the shared DB in the existing review UI.
3. Let the owner approve exact rows/messages, but keep Work.ua as manual
   handoff until a separate browser adapter is reviewed.
4. If a future apply adapter is added, implement prepare and final submit as
   separate gates and keep resume upload behind a separate exact-resume approval.
