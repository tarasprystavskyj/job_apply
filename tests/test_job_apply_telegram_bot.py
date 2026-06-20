from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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


class NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self) -> None:
        pass


class StopRun(Exception):
    pass


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

    def test_approve_and_send_latest_command_approves_supported_application_rows(self) -> None:
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
            self.assertTrue(any("Approved and started all-site application batch" in text for text in sent))
            self.assertTrue(any("http://127.0.0.1:8097/" in text for text in sent))

    def test_approve_latest_alias_uses_batch_approval_flow(self) -> None:
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
            self.assertTrue(any("Approved and started all-site application batch" in text for text in sent))

    def test_approve_and_send_latest_skips_djinni_inbox_review_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
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
            with batch.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {
                        "site": "djinniinbox",
                        "url": "https://djinni.co/my/inbox/25880672/#last",
                        "title": "Recruiter reply",
                        "company": "Example",
                        "message": "Thanks for your reply.",
                        "message_file": "",
                        "salary_usd": "",
                        "linkedin": "",
                        "resume_policy": "no_resume",
                        "approved_resume_name": "",
                        "approved_to_submit": "false",
                        "final_submit_allowed": "false",
                    }
                )
            sent: list[str] = []
            launched: list[str] = []
            bot = job_apply_telegram_bot.TelegramBot.__new__(job_apply_telegram_bot.TelegramBot)
            bot.send = lambda _chat_id, text: sent.append(text)
            bot.run_submit_and_report = lambda batch_arg, _chat_id: launched.append(batch_arg)

            with patch.object(job_apply_telegram_bot.threading, "Thread", ImmediateThread):
                bot.handle_text(123, "/approve_and_send_latest", {"latest_batch": str(batch)})

            with batch.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["approved_to_submit"], "false")
            self.assertEqual(rows[0]["final_submit_allowed"], "false")
            self.assertEqual(launched, [])
            self.assertTrue(any("Review-only inbox rows skipped: 1" in text for text in sent))

    def test_chat_command_saves_search_query_for_next_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            prefs = Path(td) / "prefs.json"
            batch = Path(td) / "batch.csv"
            batch.write_text("site,url,title,company\n", encoding="utf-8")
            state = {}
            sent: list[str] = []
            query_calls: list[str] = []
            bot = job_apply_telegram_bot.TelegramBot.__new__(job_apply_telegram_bot.TelegramBot)
            bot.cfg = type("Cfg", (), {"recruiter_auto_reply_enabled": False})()
            bot.send = lambda _chat_id, text: sent.append(text)

            def fake_scan(**kwargs):
                query_calls.append(kwargs["query_text"])
                return {"batch": str(batch), "notes": {}, "errors": {}}

            with (
                patch.object(job_apply_telegram_bot, "CHAT_PREFERENCES_PATH", prefs),
                patch.object(job_apply_telegram_bot, "run_all_sources_scan", side_effect=fake_scan),
                patch.object(job_apply_telegram_bot, "format_scan_summary", return_value="scan ok"),
            ):
                bot.handle_text(123, "/чат", state)
                bot.handle_text(123, "пошук: Python FastAPI LLM remote", state)
                bot.scan(chat_id=123)

            self.assertEqual(state["chat_mode"], "config")
            self.assertEqual(query_calls, ["Python FastAPI LLM remote"])
            data = json.loads(prefs.read_text(encoding="utf-8"))
            self.assertEqual(data["search_query"], "Python FastAPI LLM remote")
            self.assertTrue(any("Config chat enabled" in text for text in sent))
            self.assertTrue(any("search_query=Python FastAPI LLM remote" in text for text in sent))

    def test_run_survives_telegram_polling_timeout(self) -> None:
        bot = job_apply_telegram_bot.TelegramBot.__new__(job_apply_telegram_bot.TelegramBot)
        bot.scheduler_loop = Mock()
        calls = {"count": 0}

        def fake_poll(_state, timeout=25):
            calls["count"] += 1
            if calls["count"] == 1:
                raise job_apply_telegram_bot.requests.exceptions.ReadTimeout("telegram timeout")
            raise StopRun()

        bot.poll_once = fake_poll

        with (
            patch.object(job_apply_telegram_bot, "load_state", return_value={}),
            patch.object(job_apply_telegram_bot.threading, "Thread", NoopThread),
            patch.object(job_apply_telegram_bot.time, "sleep"),
        ):
            with self.assertRaises(StopRun):
                bot.run()

        self.assertEqual(calls["count"], 2)

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

    def test_current_jobs_summary_uses_only_current_job_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            current_log = Path(td) / "dou_submit.jsonl"
            current_log.write_text(
                json.dumps(
                    {
                        "site": "dou",
                        "company": "Current",
                        "source_url": "https://jobs.dou.ua/companies/current/vacancies/1/",
                        "result": "already_applied",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            jobs = [{"site": "dou", "rows": 1, "jsonl_log": str(current_log)}]

            with patch.object(
                job_apply_telegram_bot,
                "latest_submission_blocker_summary",
                return_value="Submission blockers:\n- [DOU] Old: blocked_fill_failed",
            ):
                summary = job_apply_telegram_bot.current_jobs_summary(jobs)

            self.assertIn("already_applied=1", summary)
            self.assertIn("Current batch blockers: none.", summary)
            self.assertNotIn("Old", summary)

    def test_current_jobs_summary_reports_current_blocker_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            current_log = Path(td) / "dou_submit.jsonl"
            current_log.write_text(
                json.dumps(
                    {
                        "site": "dou",
                        "company": "Current",
                        "source_url": "https://jobs.dou.ua/companies/current/vacancies/1/",
                        "result": "blocked_fill_failed",
                        "filled": {"reason": "textarea not found"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            jobs = [{"site": "dou", "rows": 1, "jsonl_log": str(current_log)}]

            summary = job_apply_telegram_bot.current_jobs_summary(jobs)

            self.assertIn("[DOU] Current: blocked_fill_failed; textarea not found", summary)

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
