from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import job_apply_telegram_bot  # noqa: E402


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self) -> None:
        self.target(*self.args, **self.kwargs)


class TelegramBotCommandTests(unittest.TestCase):
    def write_batch(self, path: Path) -> None:
        fields = [
            "site",
            "url",
            "title",
            "company",
            "message",
            "message_file",
            "salary_usd",
            "linkedin",
            "resume_policy",
            "approved_resume_name",
            "approved_to_submit",
            "final_submit_allowed",
        ]
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerow(
                {
                    "site": "dou",
                    "url": "https://jobs.dou.ua/companies/example/vacancies/12345/",
                    "title": "Python Engineer",
                    "company": "Example",
                    "message": "Hi team, I am interested in this role.",
                    "message_file": "",
                    "salary_usd": "3000",
                    "linkedin": "https://www.linkedin.com/in/taras-prystavskyj/",
                    "resume_policy": "no_resume",
                    "approved_resume_name": "",
                    "approved_to_submit": "false",
                    "final_submit_allowed": "false",
                }
            )

    def test_approve_and_send_latest_command_approves_rows_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            self.write_batch(batch)
            sent: list[str] = []
            launched: list[str] = []
            bot = job_apply_telegram_bot.TelegramBot.__new__(job_apply_telegram_bot.TelegramBot)
            bot.send = lambda _chat_id, text: sent.append(text)
            bot.run_submit_and_report = lambda batch_arg, _chat_id: launched.append(batch_arg)

            with patch.object(job_apply_telegram_bot.threading, "Thread", ImmediateThread):
                bot.handle_text(123, "/approve_and_send_latest", {"latest_batch": str(batch)})

            with batch.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["approved_to_submit"], "true")
            self.assertEqual(rows[0]["final_submit_allowed"], "true")
            self.assertEqual(launched, [str(batch)])
            self.assertTrue(any("Approved and started all-site batch" in text for text in sent))

    def test_approve_latest_alias_uses_renamed_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            self.write_batch(batch)
            sent: list[str] = []
            launched: list[str] = []
            bot = job_apply_telegram_bot.TelegramBot.__new__(job_apply_telegram_bot.TelegramBot)
            bot.send = lambda _chat_id, text: sent.append(text)
            bot.run_submit_and_report = lambda batch_arg, _chat_id: launched.append(batch_arg)

            with patch.object(job_apply_telegram_bot.threading, "Thread", ImmediateThread):
                bot.handle_text(123, "/approve_latest", {"latest_batch": str(batch)})

            self.assertEqual(launched, [str(batch)])
            self.assertTrue(any("Approved and started all-site batch" in text for text in sent))

    def test_latest_submission_blocker_summary_includes_all_site_logs(self) -> None:
        events = [
            {
                "site": "workua",
                "source_url": "https://www.work.ua/jobs/8170878/",
                "company": "Netpeak",
                "result": "blocked_validation",
                "blocked_reason": "linkedin is required",
                "attempted_at": "2026-06-12T18:10:00+0300",
            },
            {
                "site": "dou",
                "source_url": "https://jobs.dou.ua/companies/example/vacancies/12345/",
                "company": "Example DOU",
                "result": "blocked_no_apply_form",
                "blocked_reason": "apply form not visible",
                "attempted_at": "2026-06-12T18:09:00+0300",
            },
        ]

        with patch.object(job_apply_telegram_bot, "submission_log_events", return_value=events):
            summary = job_apply_telegram_bot.latest_submission_blocker_summary()

        self.assertIn("[Work.ua] Netpeak", summary)
        self.assertIn("[DOU] Example DOU", summary)

    def test_sync_web_state_latest_batch_updates_parallel_web_interface(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "web_state.json"
            batch = Path(td) / "batch.csv"
            batch.write_text("site,url\n", encoding="utf-8")

            with patch.object(job_apply_telegram_bot, "WEB_STATE_PATH", state):
                job_apply_telegram_bot.sync_web_state_latest_batch(batch, "telegram scan")

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["latest_batch"], str(batch))
            self.assertEqual(payload["status"], "telegram scan")


if __name__ == "__main__":
    unittest.main()
