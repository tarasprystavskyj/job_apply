# Job Application Automation Safety Gates

This agent automates browser work around employment applications. That means it
can transmit sensitive personal data and affect the owner's professional
reputation. The default mode is conservative.

## Allowed Without Further Approval

- Read public job listing pages at a low rate.
- Save vacancy observations to local JSONL artifacts.
- Classify fit and deduplicate vacancies.
- Draft cover letters locally.
- Prepare a browser form up to, but not including, final submission.
- Produce a review queue for the owner.

## Requires Explicit Owner Approval

Each concrete application requires approval that names:

- site;
- vacancy URL or ID;
- resume/CV/profile file or account profile to use;
- cover letter/message text;
- whether to click the final submit/apply/send button.

## Never Do

- Do not bypass login walls, CAPTCHAs, anti-bot systems, paywalls, or access
  controls.
- Do not create, modify, or delete site accounts without explicit approval.
- Do not send generic mass applications.
- Do not lie about skills, location, employment eligibility, salary, or
  availability.
- Do not use secrets, cookies, passwords, or personal files unless the owner
  explicitly provides them for this task.
- Do not run tight polling loops. Prefer official alerts/feeds where available,
  and otherwise use low-rate browser checks with jitter and backoff.

## First Implementation Shape

1. `discover`: collect public vacancies from configured searches.
2. `normalize`: write `job.vacancy_observation.v0` JSONL records.
3. `rank`: compare vacancy text with owner-provided criteria.
4. `draft`: create local application drafts.
5. `review`: show queue and wait for owner decision.
6. `prepare`: open site form and fill approved fields.
7. `submit`: gated action only after explicit owner approval.
