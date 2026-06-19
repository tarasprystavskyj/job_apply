# Agent Installation and Onboarding Guide

Use this guide when installing the project on another local computer.

## Safety Rules

- Do not read `.env`, browser cookies, saved browser profiles, saved passwords,
  or private resume contents unless the human explicitly approves it.
- Do not click final submit/apply/send without row-level approval.
- Do not upload a resume unless the human explicitly approves the exact resume.
- Do not reject an inbound offer or change account/profile settings unless the
  human explicitly requests that concrete action. The web UI profile-on button
  counts as explicit approval for that one profile toggle attempt.
- Before any final submit/send, scan the exact outbound cover letter or
  recruiter reply text. Fail closed if it contains another person's name,
  filename-like resume references such as `.pdf` or `.doc`, or known
  contamination such as `Stanislav_Shcherbak`.
- Do not send internal/agentic phrases to employers or recruiters, including
  "review artifacts", "diffs", "scoped tool use", "I would position",
  "I should be transparent", or `Relevant resume: <filename>`.
- Quote or rephrase job titles in outbound messages, and sanitize them before
  use. Block or clean titles contaminated with company, location, or
  description text.
- Salutations must not produce duplicated wording such as "team team". If the
  company is empty, use `Hi,` or `Hi hiring team,`.
- If outbound-message QA fails, create a blocker artifact with the failed
  patterns and manual/auto-clean options instead of sending.
- Keep generated runtime data in ignored folders: `data/job_waves/`,
  `data/private/`, `tmp/`.
- For `graph-ui/` work, every new owner-requested feature must be added to the
  current project graph as a node and connected by an edge to the relevant
  implementation/agent branch. Mark new graph feature nodes as unread/new until
  the owner clicks them in the graph UI.

## Installation Steps

1. Clone the repository:

   ```powershell
   git clone https://github.com/tarasprystavskyj/job_apply.git
   cd job_apply
   ```

2. Create Python environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Create `.env` from `.env.example`.

4. Ask the human for:

   - Path to the folder with resumes.
   - Telegram bot token, or permission to create one through BotFather.
   - Telegram chat id, or permission to ask them to send `/start` to the bot and
     discover the id with `python src\job_apply_telegram_status.py`.
   - Location priorities: onsite/hybrid/remote cities and countries.
   - Default salary expectation.
   - LinkedIn profile URL.
   - Whether existing selected Djinni resume may be used.
   - Which sites may be automated: Djinni only by default.
   - Whether Djinni inbox offers may be scanned from the visible browser
     session.
   - Whether profile visibility may be toggled on from the Djinni inbox page.
   - Which Djinni inbox recruiter-response threads should be tracked daily.

5. Fill `.env`:

   ```text
   JOB_APPLY_TELEGRAM_BOT_TOKEN=
   JOB_APPLY_TELEGRAM_CHAT_ID=
   JOB_APPLY_RESUME_DIR=
   JOB_APPLY_LOCATION_PREFERENCES=
   JOB_APPLY_RECRUITER_RESPONSE_THREADS=
   JOB_APPLY_DJINNI_PROFILE_UPDATE_URL=https://djinni.co/my/profile/
   ```

6. Build local resume index:

   ```powershell
   python src\resume_index.py
   ```

7. Start web UI:

   ```powershell
   python src\job_apply_web.py
   ```

8. Open `http://127.0.0.1:8097/`.

## Human Onboarding Questions

Ask exactly enough to configure the assistant:

1. Where is your resume folder?
2. Which resume variants may be used for applications?
3. What is your LinkedIn URL?
4. What salary range should be used by default?
5. Which locations and work formats do you prefer?
6. Which roles, stacks, and seniority levels should be prioritized?
7. Which keywords or companies should be blacklisted?
8. Is Djinni logged in as a candidate in Chrome with remote debugging enabled?
9. Do you want Telegram daily notifications at 10:00?
10. Do you approve automatic submission only after checkbox approval in the UI?

## Verification

Before every commit, inspect the current diff and test the functionality touched
by that diff, not only the edited lines. If a function signature, caller, UI
route, button, scheduler path, or submit path changed, exercise all affected
callers and user-visible buttons where this can be done safely. Use a subagent
for independent verification when the change touches browser automation,
submission, Telegram, scheduling, or account-state actions.

Minimum local checks:

```powershell
python -m py_compile src\*.py
python src\resume_index.py
python src\job_apply_telegram_status.py
```

Add targeted checks based on the diff, for example:

- Web UI changes: load `http://127.0.0.1:8097/`, exercise affected buttons via
  HTTP/browser, and verify the page status/log output.
- Telegram changes: test command handling without sending applications unless
  rows are explicitly approved.
- Djinni inbox changes: run scan/profile-toggle paths only under their safety
  gates and verify output JSONL/batch rows.
- Submitter changes: run dry-run first; run execute only for explicitly
  approved rows and report per-row outcomes.

For browser submission, use dry-run first:

```powershell
python src\djinni_csv_apply.py --csv examples\approved_jobs_sample.csv
```

Run `--execute` only for rows the human has explicitly approved for live
submission. Live send testing is expected after approval, but each row still
needs exact URL, exact outbound text, and platform-specific resume/upload gates.

## Proactive Blocker Handling

When an approved application is blocked, inspect the blocker immediately and
try to remove the blocker safely instead of only reporting failure. Examples:

- Site markup changed and a selector no longer finds the apply button.
- Djinni requires profile updates before applying.
- A new validation message appears on the application form.
- A supported site changes field names or confirmation flow.
- A discovery row contains a search/listing URL, but the live submitter needs
  an exact vacancy URL. Fetch/parse the public listing once, derive exact
  vacancy URLs where visible, and rebuild the review/approval artifact before
  declaring the row blocked.
- A public vacancy URL needs a platform-specific application URL such as
  `/apply?newApply=true`. Try the documented safe apply URL transformation and
  inspect the visible form before declaring that no apply form exists.

Do not bypass approval gates. Profile/account changes, uploads, final sends,
rejections, and final submits still require explicit human approval.
Resume uploads require exact row-level approval naming the resume file, an
approved resume path/name, `upload_allowed=true`, and the platform-specific
CLI/UI final action gate. Agents may implement or test uploader code with
fixtures, but they must not read private resume contents or upload a real file
unless the human approved that exact file for that exact vacancy.

For every platform live worker, blocker handling is part of the normal loop:

1. Classify the blocker as `data`, `selector`, `navigation`, `auth`,
   `validation`, `profile`, `upload`, or `site_changed`.
2. Attempt one bounded safe remediation that does not submit, upload, change
   account state, or read secrets/private files.
3. Re-run dry-run or pre-submit validation for the affected path.
4. If remediation requires code changes, delegate a narrow patch to a subagent,
   run tests, and notify the human.
5. If still blocked, write a concrete blocker reason plus the next human action
   needed.

When a Djinni profile-update blocker appears, the Telegram bot should provide
the profile URL and offer to prepare a profile-update draft. Do not save profile
fields until the human approves the exact target positioning, salary/location
values, and final save. Default draft positioning for Taras is:
`position=Senior Python / AI Automation Engineer`, `salary=3000 USD`,
`experience=More than 10 years`,
`LinkedIn=https://www.linkedin.com/in/taras-prystavskyj/`,
`locations=Lviv onsite/hybrid preferred; Kyiv remote; USA/EU remote`, and
`skills=Python, AI automation, LLM, Backend, FastAPI, API integrations,
PostgreSQL, Docker, Playwright/Selenium, Telegram bots, GitHub Actions`.
Approval template: `Approve Djinni profile update draft; final save allowed=<yes/no>`.

If the blocker appears to be a code/selector/browser-flow issue, delegate a
bounded patch to a subagent using `gpt-5.5` with high reasoning when available.
The subagent must have a narrow write scope and must not perform real
submit/send/upload/profile/reject actions. After the patch, notify the human via
Telegram that code was modified to remove an application blocker, including the
blocked company, URL, files changed, and tests run.

Use this guarded notification form:

```powershell
python src\job_apply_telegram_bot.py --notify-code-change "<short blocker-fix notice>" --i-understand-this-sends-telegram
```

Every blocker-fix version must repeat the verification flow before commit:
inspect the diff, run safe targeted tests for every touched path, use an
independent subagent for QA when browser automation/submission/Telegram/account
state is touched, then commit and push only after tests pass.

## Djinni Inbox Workflow

Use:

```powershell
python src\djinni_inbox_scan.py
```

This opens `https://djinni.co/my/inbox/` through Chrome CDP, extracts visible
inbound offer cards/threads, scores them, and writes
`data/job_waves/djinni_inbox_offers.jsonl`.

To toggle the Djinni profile on, the human must explicitly request it or click
the web UI button. CLI form:

```powershell
python src\djinni_inbox_scan.py --execute-profile-toggle --i-understand-this-changes-djinni-profile
```

Rows with `recommendation=reject_candidate` are recommendations only. Do not
click rejection controls without a separate approved rejection workflow naming
the exact thread URL.

## Recruiter Response Workflow

Use:

```powershell
python src\recruiter_response_scan.py --thread-url <approved-thread-url>
```

This reads visible recruiter messages from approved Djinni inbox thread URLs and
writes `data/job_waves/recruiter_responses.jsonl`. It is read-only: do not
reply, archive, reject, report hire, or otherwise mutate thread state.

Daily Telegram scans should include a short response summary and two options for
how to apply lessons from rejections: conservative targeting or positioning
upgrade.

After a rejection, the scanner may draft a short thank-you reply:

```powershell
python src\recruiter_response_scan.py --thread-url <approved-thread-url> --draft-thank-you
```

Do not send that reply unless the human separately approves the exact thread,
the exact message, and final send. The guarded send command is:

```powershell
python src\recruiter_response_scan.py --thread-url <approved-thread-url> --send-thank-you --message "<exact approved message>" --i-understand-this-sends-recruiter-message
```
