# Job Application Artifact Contract

Created: 2026-06-11

All operational artifacts are append-only unless explicitly marked as generated
drafts. Records should be JSON Lines where each line is one complete JSON
object.

## `job.vacancy_observation.v0`

Purpose: a public vacancy seen during low-rate discovery.

Required fields:

- `schema`: fixed string `job.vacancy_observation.v0`
- `observed_at`: ISO-8601 timestamp with timezone if known
- `source_site`: `djinni`, `dou`, `workua`, `robotaua`, or `other`
- `source_url`: canonical vacancy or listing URL
- `title`: vacancy title
- `company`: company name if public
- `location`: location or remote status if public
- `published_hint`: human-readable freshness hint if known
- `summary`: short public summary
- `requirements`: array of public requirements
- `fit_tags`: array of matching profile tags
- `risk_flags`: array of concerns or stop points
- `status`: `observed`, `shortlisted`, `rejected`, `drafted`, `approved`, `submitted`

Optional fields:

- `salary_hint`
- `employment_type`
- `language_hint`
- `source_query`
- `notes`

## `job.application_draft.v0`

Purpose: a local draft prepared for owner review.

Required fields:

- `schema`: fixed string `job.application_draft.v0`
- `created_at`: ISO-8601 timestamp with timezone if known
- `vacancy_url`: source vacancy URL
- `source_site`: source site
- `company`: company name
- `title`: vacancy title
- `resume_artifact`: local generated resume path
- `message_language`: `en`, `uk`, or `mixed`
- `cover_letter`: exact draft text
- `personal_data_included`: boolean
- `submission_allowed`: boolean, default `false`
- `approval_status`: `needs_owner_review`, `approved_to_prepare`, `approved_to_submit`, `rejected`

Required invariant:

- `submission_allowed` must remain `false` until owner gives explicit
  per-vacancy approval naming the vacancy, payload, resume/profile, and final
  submit permission.

## File Layout

- `data/resumes/`: generated resume variants.
- `data/job_waves/`: vacancy observations, draft letters, and review queues.
- `tmp/`: extracted text, scratch files, and non-source intermediate artifacts.

## Sensitive Data Policy

Do not write secrets, cookies, saved browser profile data, passwords, or private
account exports into artifacts. Resume/contact data may appear only in owner
approved generated drafts.
