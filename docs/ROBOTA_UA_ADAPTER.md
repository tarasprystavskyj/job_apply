# Robota.ua Adapter Slice

Created: 2026-06-11

This slice is discovery/draft/status only. It does not log in, inspect cookies,
read browser profiles, upload resumes, click apply buttons, or submit forms.

## Pipeline Gates

1. `discover`
   - Build low-rate public search URLs with `RobotaUaAdapter.discovery_urls`.
   - Example: `python src\robotaua_pipeline.py discovery-urls --query "Python AI"`

2. `normalize`
   - Convert saved public HTML into `job.vacancy_observation.v0`.
   - Inputs may be public listing/detail HTML or later a public fetcher.
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
   - Owner must approve exact vacancy, resume/profile, message, and final
     action before any future Robota.ua apply adapter can act.

6. `manual_handoff`
   - Current final state for Robota.ua is manual handoff.
   - There is no prepare or final submit implementation; adapter methods raise
     `PermissionError` or `UnsupportedAction`.

7. `status_tracking`
   - Shared job/outreach/status state is written to
     `data/job_waves/job_apply_shared.sqlite3`.
   - JSONL mirror status events go to
     `data/job_waves/robotaua_status_events.jsonl`.
   - Progress snapshots go to
     `data/job_waves/robotaua_progress_snapshot.json`.

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

Refresh progress snapshot:

```powershell
python src\robotaua_pipeline.py progress
```

## Next Integration Steps

- Add a low-rate public fetcher only if the owner approves live discovery rate
  limits and query set.
- Surface `robotaua_observations.jsonl` rows in the web UI as review-only rows.
- Extend the shared DB-backed progress renderer to show platform-specific
  `manual_handoff` gates.
- Implement a prepare-only browser flow only after selectors are reviewed. It
  must stop before submit/upload and require an exact owner approval gate.
