# Job Apply Automation

Local, review-first assistant for finding relevant jobs, drafting tailored
applications, and submitting only explicitly approved Djinni applications.

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

1. Click `Підібрати свіжі вакансії`.
2. Review generated rows.
3. Tick checkboxes for vacancies you approve.
4. Click `Зберегти галочки` or `Підтвердити всі Djinni`.
5. Click `Зробити розсилку approved CSV`.
6. Watch the status and submission log tail on the page.

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
continuously. It still only submits rows already marked approved in the CSV.

## Djinni CSV Submitter

Dry-run:

```powershell
python src\djinni_csv_apply.py --csv examples\approved_jobs_sample.csv
```

Real submit:

```powershell
python src\djinni_csv_apply.py --csv path\to\approved.csv --execute --i-understand-this-submits-applications
```

Required safety columns:

- `approved_to_submit=true`
- `final_submit_allowed=true`

Rows for non-Djinni sites are ignored by the Djinni submitter and need separate
site-specific flows.

## Development Notes

Runtime artifacts are written under `data/job_waves/` and private resume
excerpts under `data/private/`. Both are ignored by git.
