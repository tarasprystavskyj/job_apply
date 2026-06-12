from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workua_csv_apply import process_row, read_rows, validate_row  # noqa: E402


FIELDNAMES = [
    "site",
    "url",
    "title",
    "company",
    "message",
    "resume_policy",
    "linkedin_policy",
    "linkedin",
    "approved_resume_name",
    "upload_allowed",
    "approved_to_submit",
    "final_submit_allowed",
]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def approved_row(**overrides: str) -> dict[str, str]:
    row = {
        "site": "workua",
        "url": "https://www.work.ua/jobs/7745352/",
        "title": "Python Engineer",
        "company": "Example",
        "message": "Hello Example, I am interested in this Python role.",
        "resume_policy": "no_upload",
        "linkedin_policy": "no_linkedin",
        "linkedin": "",
        "approved_resume_name": "",
        "upload_allowed": "false",
        "approved_to_submit": "true",
        "final_submit_allowed": "true",
    }
    row.update(overrides)
    return row


class WorkUaCsvApplyTest(unittest.TestCase):
    def test_read_rows_and_validate_approved_dry_and_live_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "approved.csv"
            write_csv(csv_path, [approved_row()])

            rows = read_rows(csv_path)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.site, "workua")
            self.assertTrue(row.approved_to_submit)
            self.assertTrue(row.final_submit_allowed)
            self.assertEqual(validate_row(row, live_action=False), [])
            self.assertEqual(validate_row(row, live_action=True), [])

    def test_live_action_requires_both_row_submit_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "blocked.csv"
            write_csv(csv_path, [approved_row(approved_to_submit="false", final_submit_allowed="false")])
            row = read_rows(csv_path)[0]

            self.assertEqual(validate_row(row, live_action=False), [])
            errors = validate_row(row, live_action=True)
            self.assertIn("approved_to_submit must be true for browser prepare/execute", errors)
            self.assertIn("final_submit_allowed must be true for browser prepare/execute", errors)

    def test_linkedin_include_policy_requires_exact_url_in_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "linkedin.csv"
            write_csv(
                csv_path,
                [
                    approved_row(
                        linkedin_policy="include_in_message",
                        linkedin="https://www.linkedin.com/in/example/",
                        message="Hello Example, please see my profile.",
                    )
                ],
            )
            row = read_rows(csv_path)[0]

            errors = validate_row(row, live_action=True)
            self.assertIn("linkedin_policy=include_in_message requires the exact linkedin URL inside message", errors)

    def test_upload_resume_is_blocked_even_with_exact_resume_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "upload.csv"
            write_csv(
                csv_path,
                [
                    approved_row(
                        resume_policy="upload_resume",
                        approved_resume_name="Exact Resume.pdf",
                        upload_allowed="true",
                    )
                ],
            )
            row = read_rows(csv_path)[0]

            errors = validate_row(row, live_action=True)
            self.assertIn("resume file upload is not implemented by this Work.ua adapter", errors)

    def test_process_row_dry_run_logs_without_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "approved.csv"
            log_path = Path(tmp) / "attempts.jsonl"
            write_csv(csv_path, [approved_row()])
            row = read_rows(csv_path)[0]

            rc = process_row(
                row,
                endpoint="http://127.0.0.1:1",
                live_action=False,
                execute=False,
                log_path=log_path,
                delay=0,
            )

            self.assertEqual(rc, 0)
            event = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(event["schema"], "job.workua_submission_attempt.v0")
            self.assertEqual(event["result"], "dry_run_ok")
            self.assertEqual(event["message_length"], len(row.message))
            self.assertIn("message_sha256", event)
            self.assertNotIn(row.message, log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
