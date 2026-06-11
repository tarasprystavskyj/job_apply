# Work.ua Adapter Slice

Created: 2026-06-11

This slice is discovery, normalization, scoring, drafting, review, manual
handoff, and status tracking only. It does not log in, inspect cookies, read
browser profiles, upload resumes, click apply buttons, submit forms, or change
Work.ua account state.

## Files

- `src/job_platforms/workua.py`: Work.ua adapter, public HTML extractor, shared
  DB helpers, run-once public search helper, artifact writer, and graph progress
  snapshot.
- `src/workua_public_run_once.py`: bounded CLI wrapper for public search URL
  fetch/read and review-only artifact generation.
- `src/job_platforms/platforms.py`: registry-facing import for `WorkUaAdapter`.
- `tests/test_workua_adapter.py`: safe parser, gate, shared DB, and progress
  coverage.

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
   - Current final path is manual handoff only.
   - The adapter does not implement prepare or submit. Base methods raise
     `PermissionError` before approval and `UnsupportedAction` after approval.
   - A future executable Work.ua flow must remain behind
     `approved_to_submit=true`, `final_submit_allowed=true`, and exact approved
     message text.

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
