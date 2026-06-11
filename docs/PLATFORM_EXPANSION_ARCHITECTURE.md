# Platform Expansion Architecture

Created: 2026-06-11

This layer prepares Robota.ua, Work.ua, and DOU expansion without changing the
current Djinni submitter, Telegram bot, or web approval behavior.

## Safe Extension Points

- `src/job_platforms/models.py`: shared dataclasses for observations, resume
  metadata, outreach drafts, approval gates, and progress graph snapshots.
- `src/job_platforms/base.py`: adapter contract plus safety gates. Discovery
  and local drafting are allowed by default. Form preparation and final submit
  fail closed until a concrete adapter implements them behind explicit owner
  approval.
- `src/job_platforms/platforms.py`: Robota.ua and Work.ua adapter templates
  with public discovery URL builders.
- `src/job_platforms/dou.py`: DOU / Relocate DOU public discovery,
  normalization, local scoring, review-only draft, shared DB, and progress
  graph helpers.
- `src/shared_job_db.py`: shared SQLite schema and helpers for resume metadata,
  jobs, outreach drafts, statuses, and append-only events.
- `src/job_platforms/progress.py`: graph-shaped `job.progress_snapshot.v0`
  builder for a web UI or LangGraph-like progress visualization.

## Database Scope

Default DB path:

```text
data/job_waves/job_apply_shared.sqlite3
```

The DB is runtime state under an ignored directory. It stores resume metadata
only: display name, artifact path, digest, tags, and status. It must not store
private CV text, cookies, browser profiles, passwords, or account exports.

Tables:

- `resumes`: shared resume metadata.
- `jobs`: normalized public vacancy observations.
- `outreach`: local draft messages and approval flags.
- `statuses`: status history for jobs/outreach/resumes.
- `events`: append-only timeline for progress views and audit.

## Approval Boundary

Adapters must preserve these gates:

- Discovery and local drafting are safe by default.
- Preparing a form requires `ApprovalGate.approved_to_prepare` or
  `ApprovalGate.approved_to_submit`.
- Final submit requires both `approved_to_submit=true` and
  `final_submit_allowed=true`, plus exact approved message text.
- Resume upload is not enabled by the base adapter and requires a separate
  exact-resume approval gate.

Current Robota.ua, Work.ua, and DOU adapters do not click, type, upload, send,
submit, or change account state. DOU has public parsing and shared DB review
draft helpers, but final application remains manual handoff.
