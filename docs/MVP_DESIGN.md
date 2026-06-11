# Job Application Assistant MVP Design

Created: 2026-06-11
Mode: dry-run, review-first

## Goal

Build a safe browser-assisted job application pipeline for Taras Prystavskyj.
The MVP observes public vacancies, ranks fit, drafts tailored messages, and
prepares a review queue. It must not submit, upload, or transmit personal data
without explicit approval for a concrete vacancy and payload.

## Target Roles

Initial wave focus:

- AI Integration Engineer
- AI Automation Developer
- Python AI Engineer
- Backend Engineer with AI/LLM integrations
- Technical Product Builder for AI-enabled workflows
- Algorithmic trading / automation engineer when Python and research tooling are central

## Sites

- Djinni: public job pages and keyword pages. Stop at login/apply gates.
- DOU / Relocate DOU: public listings and job detail pages. Stop at application forms.
- Work.ua: public listing and detail pages. Stop at login/apply gates.
- Robota.ua: public listing and detail pages. Stop at login/apply gates.

## Discovery Strategy

Start with manual/low-rate public discovery:

- Query by role terms: `Python AI`, `LLM`, `Agentic AI`, `AI Automation`,
  `FastAPI AI`, `RAG`, `AI Integration`, `Python automation`.
- Prefer fresh listings, remote/Lviv/Ukraine/EU-compatible jobs, and roles where
  founder/product/technical-lead experience is an advantage.
- Avoid jobs requiring factual skills not supported by the resume.
- Record each candidate as an append-only vacancy observation.

## Resume Inputs

Approved local source directory supplied by owner:

- `C:\python_scripts\projects_search\my_resumes`

Current primary resume basis:

- `Taras_Prystavskyj_AI_Integration_Resume.pdf`

Structural reference only, not factual source:

- `Stanislav_Shcherbak_ai_dev_eng (1).pdf`

Generated resume artifacts remain in this project unless the owner explicitly
asks to overwrite source resume files.

## Approval Workflow

For each application:

1. Record vacancy observation.
2. Select resume variant.
3. Draft personalized cover letter.
4. Present exact vacancy URL, resume path, and message text to owner.
5. Wait for explicit approval naming:
   - site;
   - vacancy URL or ID;
   - resume file/profile to use;
   - final cover letter text;
   - whether final submit/send/apply is allowed.

## Browser Automation Boundary

Allowed after review:

- Open owner-visible public vacancy pages.
- Fill fields with approved data.
- Stop before final submit/send/apply.

Not allowed without explicit per-vacancy approval:

- Upload resume.
- Send cover letter.
- Transmit phone/email/profile data.
- Click final submit/apply/send.

Never allowed:

- CAPTCHA bypass.
- Login-wall bypass.
- Anti-bot evasion.
- Paid feature or subscription changes.
- Account creation or settings changes without explicit approval.

## Smoke Test

Smallest safe smoke test:

1. Read a local JSONL fixture or public listing search result.
2. Normalize one vacancy into `job.vacancy_observation.v0`.
3. Rank against approved resume summary.
4. Generate one local `job.application_draft.v0`.
5. Verify no browser submit/upload action is present.

## Owner Inputs Still Needed

- Confirm preferred primary role order.
- Confirm salary expectations and minimum acceptable rate.
- Confirm remote/hybrid/location constraints.
- Confirm languages and seniority positioning.
- Confirm which generated resume PDF may be used.
- Confirm whether Djinni/DOU/Work.ua/Robota.ua accounts are already logged in and which must remain manual.
