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
- Read visible Djinni inbox offer summaries from an already logged-in,
  owner-visible browser session when the owner has requested inbox scanning.
- Send low-risk Djinni recruiter replies only when recruiter auto-reply mode is
  explicitly enabled, confidence is at least the configured threshold, and the
  reply is one of the allowlisted intents: polite rejection thank-you or public
  resume/profile link response using `JOB_APPLY_PUBLIC_RESUME_LINKS`.

## Requires Explicit Owner Approval

Each concrete application requires approval that names:

- site;
- vacancy URL or ID;
- resume/CV/profile file or account profile to use;
- cover letter/message text;
- whether to click the final submit/apply/send button.

Changing account/profile visibility or rejecting an inbound offer also requires
explicit approval naming the concrete action and target. A local UI button with
the action name counts as approval for that one click.

Recruiter replies that involve salary, availability, scheduling, interviews,
test tasks, negotiation, factual uncertainty, or non-allowlisted attachments
require explicit owner approval even if a draft is generated.

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
8. `inbox`: scan visible Djinni inbox offers, score them, and add relevant or
   review/reject-candidate rows to the daily review queue.
9. `recruiter_reply`: classify visible recruiter messages; auto-reply only for
   allowlisted high-confidence replies, then notify the owner in Telegram.
