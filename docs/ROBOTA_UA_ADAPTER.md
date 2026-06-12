# Robota.ua Adapter Slice

Created: 2026-06-11

This slice keeps discovery/draft/status review-first and adds a separate guarded
CSV live path in `src\robotaua_csv_apply.py`. The live path attaches only to an
already-running Chrome CDP endpoint. It does not inspect cookies, read browser
profiles, saved passwords, or local CV contents.

## Pipeline Gates

1. `discover`
   - Build low-rate public search URLs with `RobotaUaAdapter.discovery_urls`.
   - Example: `python src\robotaua_pipeline.py discovery-urls --query "Python AI"`
   - Run-once live discovery is limited to HTTPS `robota.ua` `/zapros/`
     public search pages. It does not use login, cookies, browser profiles, or
     vacancy application forms.

2. `normalize`
   - Convert saved public HTML into `job.vacancy_observation.v0`.
   - Inputs may be saved public listing/detail HTML or bounded public
     `run-once` search-page fetch output.
   - Output: `data/job_waves/robotaua_observations.jsonl`.

3. `score`
   - Use the shared vacancy scoring shape from `vacancy_pipeline.score_vacancy`.
   - Robota.ua rows are penalized slightly for `manual_handoff_only` so Djinni
     executable rows remain separate from manual review rows.

4. `draft`
   - Generate local `job.application_draft.v0` rows in
     `data/job_waves/robotaua_outreach_drafts.jsonl`.
   - Resume integration is metadata-only at this stage. The code anticipates
     the shared resume index but intentionally does not read private CV excerpts
     during this safe adapter slice.

5. `review_approval`
   - Review CSV rows use `approved_to_submit=false` and
     `final_submit_allowed=false`.
   - Review CSV rows include `resume_policy`, `linkedin_policy`, and
     `upload_allowed` columns so the owner can create a structured live gate.
   - Owner must approve exact vacancy URL, exact message, resume/LinkedIn
     policy, `approved_to_submit=true`, and `final_submit_allowed=true` before
     the live path can prepare or submit a form.
   - Draft and blocker records include this approval text:
     `Approve Robota.ua application draft for exact vacancy URL <url>; resume=<exact resume name>; message=<exact approved message>; final submit allowed=<yes/no>`.

6. `final_gated_apply`
   - Dry-run validation:
     `python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv`
   - Pre-submit browser preparation:
     `python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv --pre-submit --i-understand-this-prepares-robotaua-application`
   - Final submit:
     `python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv --execute --i-understand-this-submits-robotaua-application`
   - Final submit clicks are refused unless the row has `site=robotaua`, an
     HTTPS Robota.ua vacancy URL, exact `message`, `resume_policy`,
     `linkedin_policy`, `approved_to_submit=true`,
     `final_submit_allowed=true`, and the explicit CLI guard flag.
   - Base vacancy URLs are normalized to the platform apply URL variant
     `/apply?newApply=true` before the live worker opens the form.
   - Resume file upload is allowed only when the exact row approves
     `resume_policy=upload_exact_resume`, `upload_allowed=true`, and the exact
     `approved_resume_name`/approved path. The worker may use the visible
     Robota.ua attach-resume control, but must not read private resume contents
     or upload a real file unless the human approved that exact file for that
     exact vacancy.
   - If `approved_resume_path` is omitted, the worker resolves the exact file
     name inside `JOB_APPLY_RESUME_DIR`. If `approved_resume_path` is present,
     its basename must match `approved_resume_name`, and the path must stay
     inside `JOB_APPLY_RESUME_DIR`, `data/job_waves/`, or
     `data/approved_artifacts/`.
   - Current attach selector:
     `body > app-root > div > alliance-apply-page-shell > div > alliance-apply-page-new > div > div:nth-child(2) > div > lib-attach-apply-block > div > label > div`.
   - File attachment uses Chrome CDP `DOM.setFileInputFiles`, followed by a
     pre-submit verification that the selected file name matches
     `approved_resume_name`.

7. `status_tracking`
   - Shared job/outreach/status state is written to
     `data/job_waves/job_apply_shared.sqlite3`.
   - JSONL mirror status events go to
     `data/job_waves/robotaua_status_events.jsonl`.
   - Progress snapshots go to
     `data/job_waves/robotaua_progress_snapshot.json`.
   - The current run-once and review paths also write a Robota-specific
     shared-DB graph with schema `job.robotaua_progress_snapshot.langgraph.v0`.

## LangGraph-Like Progress Snapshot

Schema: `job.progress_snapshot.langgraph.v0`

Top-level fields:

- `schema`
- `generated_at`
- `nodes`
- `edges`
- `event_tail`

Node fields:

- `id`: one of `discover`, `normalize`, `score`, `draft`,
  `review_approval`, `manual_handoff`, `status_tracking`
- `label`: UI label
- `kind`: `stage` or `gate`
- `status`: `pending`, `ready`, `complete`, `active`, or
  `blocked_waiting_owner`
- `data`: small UI-safe metadata

Edge fields:

- `source`
- `target`
- `label`
- `status`

The snapshot is intentionally UI-friendly and can be consumed by a LangGraph-like
graph renderer without exposing private resume text or account state.

## Safe Commands

Normalize a saved public page:

```powershell
python src\robotaua_pipeline.py normalize-html --html tmp\robotaua_listing.html --source-url "https://robota.ua/zapros/Python+AI" --query "Python AI"
```

Build review-only artifacts:

```powershell
python src\robotaua_pipeline.py build-review
```

Validate an owner-approved Robota.ua live CSV without opening a browser:

```powershell
python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv
```

Prepare an approved application in the visible browser session and stop before
the final click:

```powershell
python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv --pre-submit --i-understand-this-prepares-robotaua-application
```

Submit only after the same row-level gates plus the final CLI guard:

```powershell
python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv --execute --i-understand-this-submits-robotaua-application
```

Approved upload rows may include:

```csv
site,url,message,resume_policy,approved_resume_name,approved_resume_path,upload_allowed,linkedin_policy,approved_to_submit,final_submit_allowed
robotaua,https://robota.ua/company3685368/vacancy11052703,Exact approved message...,upload_exact_resume,Senior Python CV.pdf,,true,omit_linkedin,true,true
```

Fetch one public search page, normalize vacancies, create review-only DB drafts,
and refresh the graph:

```powershell
python src\robotaua_pipeline.py run-once --query "Python AI" --limit 10 --max-pages 1 --delay-seconds 2
```

Refresh progress snapshot:

```powershell
python src\robotaua_pipeline.py progress
```

## Next Integration Steps

- Surface `robotaua_observations.jsonl` rows in the web UI as review-only rows.
- Extend the shared DB-backed progress renderer to show platform-specific
  `manual_handoff` gates.
- Review Robota.ua selectors against a real approved vacancy before first final
  submit. Use `--pre-submit` first and inspect
  `data/job_waves/robotaua_submission_attempts.jsonl`.
- Keep exact-resume file upload behind the row-level approval gate and a
  pre-submit validation pass before any final click.
- When an approved Robota.ua live action is blocked by markup or navigation,
  patch the adapter selector/navigation path and re-run dry-run plus
  pre-submit before reporting manual handoff.
