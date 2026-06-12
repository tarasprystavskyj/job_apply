from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from djinni_csv_apply import ApplicationRow, main, process_row, read_rows, validate_row  # noqa: E402


APPROVED_MESSAGE = (
    "Hi team, I am interested in this Djinni role because it matches my Python, "
    "automation, and production backend experience. I can help with delivery, "
    "integration work, and pragmatic AI automation while keeping communication clear."
)


def make_row(**overrides: object) -> ApplicationRow:
    values = {
        "row_number": 2,
        "site": "djinni",
        "url": "https://djinni.co/jobs/123456-python-engineer/",
        "title": "Python Engineer",
        "company": "Example Co",
        "message": APPROVED_MESSAGE,
        "salary_usd": "3000",
        "linkedin": "https://www.linkedin.com/in/example/",
        "resume_policy": "no_resume",
        "approved_resume_name": "",
        "answers": {},
        "approved_to_submit": True,
        "final_submit_allowed": True,
    }
    values.update(overrides)
    return ApplicationRow(**values)  # type: ignore[arg-type]


class DjinniCsvApplyTests(unittest.TestCase):
    def test_valid_live_ready_row_passes_validation(self) -> None:
        self.assertEqual(validate_row(make_row(), execute=False), [])
        self.assertEqual(validate_row(make_row(), execute=True), [])

    def test_validation_requires_full_structured_gate_data_even_in_dry_run(self) -> None:
        errors = validate_row(
            make_row(
                linkedin="",
                approved_to_submit=False,
                final_submit_allowed=False,
            ),
            execute=False,
        )

        self.assertIn("linkedin is required", errors)
        self.assertIn("approved_to_submit must be true", errors)
        self.assertIn("final_submit_allowed must be true", errors)

    def test_message_file_is_blocked_instead_of_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "batch.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
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
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "site": "djinni",
                        "url": "https://djinni.co/jobs/123456-python-engineer/",
                        "title": "Python Engineer",
                        "company": "Example Co",
                        "message": "",
                        "message_file": "message.txt",
                        "salary_usd": "3000",
                        "linkedin": "https://www.linkedin.com/in/example/",
                        "resume_policy": "no_resume",
                        "approved_resume_name": "",
                        "approved_to_submit": "true",
                        "final_submit_allowed": "true",
                    }
                )

            with self.assertRaisesRegex(ValueError, "message_file is not allowed"):
                read_rows(csv_path)

    def test_dry_run_writes_ok_log_without_browser_for_live_ready_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "attempts.jsonl"

            rc = process_row(
                make_row(),
                endpoint="http://127.0.0.1:1",
                execute=False,
                log_path=log_path,
                delay=0,
            )

            self.assertEqual(rc, 0)
            event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(event["result"], "dry_run_ok")
            self.assertEqual(event["site"], "djinni")
            self.assertTrue(event["approved_to_submit"])
            self.assertTrue(event["final_submit_allowed"])
            self.assertFalse(event["execute"])

    def test_execute_requires_cli_confirmation_flag(self) -> None:
        rc = main(["--csv", "does-not-need-to-exist.csv", "--execute"])

        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
