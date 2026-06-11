# Agent Installation and Onboarding Guide

Use this guide when installing the project on another local computer.

## Safety Rules

- Do not read `.env`, browser cookies, saved browser profiles, saved passwords,
  or private resume contents unless the human explicitly approves it.
- Do not click final submit/apply/send without row-level approval.
- Do not upload a resume unless the human explicitly approves the exact resume.
- Keep generated runtime data in ignored folders: `data/job_waves/`,
  `data/private/`, `tmp/`.

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

5. Fill `.env`:

   ```text
   JOB_APPLY_TELEGRAM_BOT_TOKEN=
   JOB_APPLY_TELEGRAM_CHAT_ID=
   JOB_APPLY_RESUME_DIR=
   JOB_APPLY_LOCATION_PREFERENCES=
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

Run:

```powershell
python -m py_compile src\*.py
python src\resume_index.py
python src\job_apply_telegram_status.py
```

For browser submission, use dry-run first:

```powershell
python src\djinni_csv_apply.py --csv examples\approved_jobs_sample.csv
```

Do not run `--execute` until the human has approved specific rows.
