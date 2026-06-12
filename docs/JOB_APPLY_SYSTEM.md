# Job Apply System Skeleton

This is a local, review-first job application system.

Implemented entry points:

- Web UI: `python src\job_apply_web.py`
- Telegram bot: `python src\job_apply_telegram_bot.py`
- Resume index: `python src\resume_index.py`
- Batch builder: `python src\vacancy_pipeline.py`
- Djinni submitter: `python src\djinni_csv_apply.py`
- Djinni inbox scanner: `python src\djinni_inbox_scan.py`
- Robota.ua review-only adapter: `python src\robotaua_pipeline.py`
- Robota.ua guarded submitter: `python src\robotaua_csv_apply.py`

The `.env` file contains local runtime settings:

- `JOB_APPLY_TELEGRAM_BOT_TOKEN`
- `JOB_APPLY_TELEGRAM_CHAT_ID`
- `JOB_APPLY_RESUME_DIR`
- `JOB_APPLY_AGENT_MODEL=gpt-5.3-codex-spark`
- `JOB_APPLY_DAILY_HOUR=10`
- `JOB_APPLY_LOCATION_PREFERENCES=Lviv onsite preferred; Kyiv remote; USA remote`

The web UI currently builds a candidate CSV from local observed vacancies and
shows proposed links. The submit button only sends rows already marked
`approved_to_submit=true` and `final_submit_allowed=true`.

The web UI can also scan `https://djinni.co/my/inbox/` through the visible
Chrome CDP session. Inbound offers are stored in
`data/job_waves/djinni_inbox_offers.jsonl` and then included in the daily batch
as `site=djinni_inbox` review-only rows. The submitter ignores these rows.

The Telegram bot follows the AIMA-style pattern:

1. Daily scheduler checks at 10:00 local time.
2. It builds a candidate batch.
3. It sends the list to Telegram.
4. It waits for `/approve_latest`.
5. It launches the existing CSV submitter for approved rows.

During `/scan` and the daily scheduler, the bot attempts a Djinni inbox scan
first. If Chrome CDP is unavailable, it logs the inbox scan failure and still
builds the batch from existing observations.

Resume indexing reads PDF/text/markdown files from `JOB_APPLY_RESUME_DIR` and
writes private excerpts to `data/private/resume_index.json`, which is ignored by
git.

Current limitation: discovery uses local observations as seed data. The next
step is to add low-rate public-page discovery for Djinni, DOU, Work.ua, and
Robota.ua, then pass vacancy text plus resume index into the GPT-5.3-Codex-Spark
drafting agent.

Robota.ua now has a bounded adapter slice for public HTML normalization,
review-only draft generation, status events, and LangGraph-like progress
snapshots. It also has a separate guarded CSV live path that requires
row-level approval, exact message text, resume/LinkedIn policy, and an explicit
CLI guard. Resume file upload remains blocked.
