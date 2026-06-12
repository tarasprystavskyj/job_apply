# Job Apply Automation

Local, review-first assistant for finding relevant jobs, drafting tailored
applications, and submitting only explicitly approved Djinni and Robota.ua
applications.

The project is intentionally conservative:

- It does not read browser cookies, saved passwords, or browser profile files.
- It does not upload files unless a row explicitly approves an existing selected
  resume.
- It does not submit rows unless both `approved_to_submit=true` and
  `final_submit_allowed=true`.
- It stores secrets in `.env`, which is ignored by git.

## Install

```powershell
git clone https://github.com/tarasprystavskyj/job_apply.git
cd job_apply
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

```text
JOB_APPLY_TELEGRAM_BOT_TOKEN=<your Telegram bot token>
JOB_APPLY_TELEGRAM_CHAT_ID=<your Telegram chat id, can be discovered after /start>
JOB_APPLY_RESUME_DIR=C:\path\to\your\resumes
JOB_APPLY_LOCATION_PREFERENCES=Lviv onsite preferred; Kyiv remote; USA remote
JOB_APPLY_RECRUITER_RESPONSE_THREADS=<optional comma-separated Djinni inbox thread URLs>
JOB_APPLY_DJINNI_PROFILE_UPDATE_URL=https://djinni.co/my/profile/
JOB_APPLY_PUBLIC_RESUME_LINKS=<optional comma-separated public resume/profile URLs>
JOB_APPLY_RECRUITER_AUTO_REPLY_ENABLED=false
JOB_APPLY_RECRUITER_AUTO_REPLY_THRESHOLD=0.80
```

## First Run

Index local resumes:

```powershell
python src\resume_index.py
```

Start the local web UI:

```powershell
python src\job_apply_web.py
```

Open:

```text
http://127.0.0.1:8097/
```

Use the UI:

1. Click `Підібрати свіжі вакансії + Djinni inbox`.
2. Review generated rows.
3. Tick checkboxes for vacancies you approve.
4. Click `Зберегти галочки` or `Підтвердити всі Djinni`.
5. Click `Зробити розсилку approved CSV`.
6. Watch status, submission log tail, and blocked reasons on the page.

The UI also has:

- `Оновити тільки Djinni inbox`: scans `https://djinni.co/my/inbox/` in an
  already logged-in, owner-visible Chrome session and writes inbound offers to
  the local review queue.
- `Увімкнути профіль Djinni`: clicks a visible Djinni profile-on control only
  when such a control is detected on the inbox page.

Inbound Djinni offers are added to the daily batch as `site=djinni_inbox`.
They are review-only rows: the submitter will not send applications for them.
Low-fit offers are marked `recommendation=reject_candidate` so the owner can
review and approve a later rejection workflow.

## Telegram Bot

Create a Telegram bot through `@BotFather`, put the token into `.env`, then send
`/start` to your bot.

Discover bot/chat status:

```powershell
python src\job_apply_telegram_status.py
```

Run one polling pass:

```powershell
python src\job_apply_telegram_bot.py --once
```

Run continuously:

```powershell
python src\job_apply_telegram_bot.py
```

Commands:

- `/status`
- `/scan`
- `/approve_latest`

The bot sends a daily scan around `JOB_APPLY_DAILY_HOUR` when running
continuously. `/scan` first refreshes Djinni inbox offers and recruiter
responses when Chrome CDP is available, then builds the candidate batch. It
still only submits rows already marked approved in the CSV.

When a submission is blocked by Djinni profile requirements, Telegram status
messages include the profile-update link from `JOB_APPLY_DJINNI_PROFILE_UPDATE_URL`.
The bot also proposes a separate approval template for preparing a Djinni
profile-update draft; profile fields must not be saved without explicit final
profile-save approval. The suggested draft values are:
`position=Senior Python / AI Automation Engineer`, `salary=3000 USD`,
`experience=More than 10 years`,
`LinkedIn=https://www.linkedin.com/in/taras-prystavskyj/`,
`locations=Lviv onsite/hybrid preferred; Kyiv remote; USA/EU remote`, and
`skills=Python, AI automation, LLM, Backend, FastAPI, API integrations,
PostgreSQL, Docker, Playwright/Selenium, Telegram bots, GitHub Actions`.
Approval template: `Approve Djinni profile update draft; final save allowed=<yes/no>`.
After `/approve_latest`, the bot runs the approved submitter in the background
and sends a completion message with the latest blockers.

## Recruiter Response Review

Configured Djinni inbox threads in `JOB_APPLY_RECRUITER_RESPONSE_THREADS` are
checked during daily scans. The bot summarizes recruiter outcomes, including
rejections, and proposes two ways to apply the lesson:

- Option A: narrow targeting toward roles with the strongest proven fit.
- Option B: keep broader senior AI targeting but strengthen profile/CV/cover
  letter positioning around concrete LLM/RAG/multi-agent deliverables.

The response scanner is read-only. It does not reply, archive, reject, or change
thread state.

Optional auto-reply mode is available for low-risk recruiter replies. It is off
by default. Enable it only after adding allowlisted public resume links:

```text
JOB_APPLY_PUBLIC_RESUME_LINKS=https://drive.google.com/file/d/.../view?usp=sharing
JOB_APPLY_RECRUITER_AUTO_REPLY_ENABLED=true
JOB_APPLY_RECRUITER_AUTO_REPLY_THRESHOLD=0.80
```

When enabled, daily scans may send an automatic reply only when the classifier
confidence is at least the threshold. Current safe auto-reply intents are:

- polite thank-you after a clear rejection;
- public resume/profile link reply when the recruiter explicitly asks for a
  resume/CV/profile link and no salary, scheduling, interview, or negotiation
  question is present.

Every attempted auto-reply is written to
`data/job_waves/recruiter_auto_replies.jsonl`. Sent messages are deduplicated by
thread, intent, and message digest. When an auto-reply is actually sent, the
Telegram bot sends a notification with the thread URL and confidence score.
Messages that ask about salary, availability, calls, interviews, test tasks, or
other decisions remain manual-review only.

To prepare a polite thank-you reply after a rejection:

```powershell
python src\recruiter_response_scan.py --thread-url <approved-thread-url> --draft-thank-you
```

Real sending is blocked unless the operator gives a separate approval naming
the exact thread and exact message. CLI form:

```powershell
python src\recruiter_response_scan.py --thread-url <approved-thread-url> --send-thank-you --message "exact approved message" --i-understand-this-sends-recruiter-message
```

## Djinni CSV Submitter

Dry-run:

```powershell
python src\djinni_csv_apply.py --csv examples\approved_jobs_sample.csv
```

The sample CSV contains a placeholder Djinni-shaped URL with approval gates set
to `false`, so dry-run should block it. A live-batch dry-run should use rows
that already contain the exact approved message, salary, LinkedIn URL,
resume-policy choice, `approved_to_submit=true`, and
`final_submit_allowed=true`.

Real submit:

```powershell
python src\djinni_csv_apply.py --csv path\to\approved.csv --execute --i-understand-this-submits-applications
```

Required safety columns:

- `site=djinni`
- `url=https://djinni.co/jobs/...`
- exact `message` in the CSV row
- `salary_usd`
- `linkedin`
- `resume_policy`
- `approved_to_submit=true`
- `final_submit_allowed=true`

Rows for non-Djinni sites are ignored by the Djinni submitter and need separate
site-specific flows. `site=djinni_inbox` rows are also ignored by the submitter
and are intended for review/reply/reject decisions only.

## Work.ua Resume Update

Generic guarded Work.ua resume edit prepare/save lives in
`src\workua_resume_update.py`. It accepts an exact Work.ua resume edit URL plus
exact approved field updates from JSON/CSV. Dry-run is the default and does not
open a browser:

```powershell
python src\workua_resume_update.py --json path\to\workua_resume_update.json
```

Browser prepare fills approved fields through Chrome CDP and stops before save:

```powershell
python src\workua_resume_update.py --json path\to\workua_resume_update.json --prepare-browser --i-understand-this-prepares-workua-resume-update
```

Final save additionally requires `final_save_allowed=true` in the config and:

```powershell
python src\workua_resume_update.py --json path\to\workua_resume_update.json --execute-save --i-understand-this-prepares-workua-resume-update --i-understand-this-saves-workua-resume-update
```

The attempt log stores field value lengths and SHA-256 digests, not full resume
text.

## Robota.ua CSV Submitter

Validation-only:

```powershell
python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv
```

Pre-submit preparation opens/fills the approved vacancy form and stops before
the final click:

```powershell
python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv --pre-submit --i-understand-this-prepares-robotaua-application
```

Real submit:

```powershell
python src\robotaua_csv_apply.py --csv path\to\approved_robotaua.csv --execute --i-understand-this-submits-robotaua-application
```

Required Robota.ua safety columns include `site=robotaua`, `url`, exact
`message`, `resume_policy`, `linkedin_policy`, `approved_to_submit=true`,
`final_submit_allowed=true`, and `upload_allowed`. Resume file uploads are still
blocked by the current Robota.ua flow.

## Development Notes

Runtime artifacts are written under `data/job_waves/` and private resume
excerpts under `data/private/`. Both are ignored by git.
