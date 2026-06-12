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

import robotaua_csv_apply as apply  # noqa: E402


EXACT_MESSAGE = (
    "Hello, I am interested in this Python automation role. "
    "My background fits the requirements, and I can discuss details."
)


def approved_row(**overrides: object) -> apply.RobotauaApplicationRow:
    data = {
        "row_number": 2,
        "site": "robotaua",
        "url": "https://robota.ua/company123/vacancy987654",
        "title": "Python Engineer",
        "company": "Example",
        "message": EXACT_MESSAGE,
        "salary_usd": "3000",
        "linkedin": "https://www.linkedin.com/in/taras-prystavskyj/",
        "linkedin_policy": "include_linkedin",
        "resume_policy": "no_resume",
        "approved_resume_name": "",
        "upload_allowed": False,
        "answers": {},
        "approved_to_submit": True,
        "final_submit_allowed": True,
    }
    data.update(overrides)
    return apply.RobotauaApplicationRow(**data)


class FakeTab:
    def __init__(self) -> None:
        self.closed = False
        self.submit_clicked = False

    def call(self, _method: str, _params: dict | None = None) -> dict:
        return {}

    def close(self) -> None:
        self.closed = True

    def eval(self, expression: str) -> dict:
        if "already_applied" in expression:
            return {
                "url": "https://robota.ua/company123/vacancy987654",
                "title": "Example",
                "already_applied": False,
                "login_required": False,
                "inactive": False,
                "controls": [{"text": "Відгукнутися"}],
                "fields": [],
                "alerts": [],
            }
        if "hasMessageField" in expression:
            return {"ok": True, "opened": True, "text": "Відгукнутися"}
        if "filledAnswers" in expression:
            return {
                "ok": True,
                "messageLength": len(EXACT_MESSAGE),
                "linkedinFields": 1,
                "linkedin": "https://www.linkedin.com/in/taras-prystavskyj/",
                "fileInputs": 0,
                "selectedResumeText": [],
            }
        if "submitControls" in expression:
            return {"ok": True, "errors": [], "details": {"submitControls": [{"text": "Надіслати", "disabled": False}]}}
        if "button.click" in expression:
            self.submit_clicked = True
            return {"ok": True, "submitted": True, "text": "Надіслати"}
        raise AssertionError("unexpected script")


class RobotauaCsvApplyTest(unittest.TestCase):
    def test_validate_row_requires_robotaua_gate_data_for_live_modes(self) -> None:
        row = approved_row(approved_to_submit=False, final_submit_allowed=False)
        errors = apply.validate_row(row, "pre_submit")
        self.assertIn("approved_to_submit must be true", errors)
        self.assertIn("final_submit_allowed must be true", errors)

    def test_validate_row_blocks_unapproved_upload_requests(self) -> None:
        row = approved_row(resume_policy="upload_exact_resume", approved_resume_name="Senior Python CV.pdf", upload_allowed=False)
        errors = apply.validate_row(row, "submit")
        self.assertIn("upload_allowed=true is required when resume_policy=upload_exact_resume", errors)

    def test_validate_row_accepts_approved_no_resume_submit_row(self) -> None:
        self.assertEqual(apply.validate_row(approved_row(), "submit"), [])

    def test_dry_run_writes_audit_log_without_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "attempts.jsonl"
            rc = apply.process_row(approved_row(), "http://127.0.0.1:9222", "dry_run", log, 0)
            self.assertEqual(rc, 0)
            event = json.loads(log.read_text(encoding="utf-8").strip())
            self.assertEqual(event["schema"], "job.robotaua_submission_attempt.v0")
            self.assertEqual(event["result"], "dry_run_ok")
            self.assertEqual(event["site"], "robotaua")

    def test_pre_submit_path_skips_final_click(self) -> None:
        fake = FakeTab()
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply.time, "sleep", lambda _seconds: None):
            log = Path(tmp) / "attempts.jsonl"
            rc = apply.process_row(
                approved_row(),
                "http://127.0.0.1:9222",
                "pre_submit",
                log,
                0,
                tab_factory=lambda _endpoint, _url: fake,
            )
            self.assertEqual(rc, 0)
            self.assertFalse(fake.submit_clicked)
            self.assertTrue(fake.closed)
            event = json.loads(log.read_text(encoding="utf-8").strip())
            self.assertEqual(event["result"], "pre_submit_ok_no_final_click")

    def test_execute_requires_explicit_cli_guard_before_reading_csv(self) -> None:
        rc = apply.main(["--csv", "does-not-exist.csv", "--execute"])
        self.assertEqual(rc, 2)

    def test_read_rows_requires_linkedin_policy_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            with path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["site", "url", "message", "resume_policy", "approved_to_submit", "final_submit_allowed"])
                writer.writeheader()
                writer.writerow(
                    {
                        "site": "robotaua",
                        "url": "https://robota.ua/company123/vacancy987654",
                        "message": EXACT_MESSAGE,
                        "resume_policy": "no_resume",
                        "approved_to_submit": "true",
                        "final_submit_allowed": "true",
                    }
                )
            with self.assertRaisesRegex(ValueError, "linkedin_policy"):
                apply.read_rows(path)


if __name__ == "__main__":
    unittest.main()
