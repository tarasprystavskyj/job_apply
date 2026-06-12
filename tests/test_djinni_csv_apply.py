from __future__ import annotations

import csv
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from djinni_csv_apply import (  # noqa: E402
    ApplicationRow,
    classify_no_apply_state,
    main,
    process_row,
    profile_update_blocker_fields,
    read_rows,
    validate_before_submit,
    validate_row,
)


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

    def test_presubmit_allows_djinni_forms_without_linkedin_input(self) -> None:
        source = inspect.getsource(validate_before_submit)

        self.assertNotIn("linkedin provided in CSV but no visible linkedin input is present", source)
        self.assertIn("linkedin was not transmitted to form", source)

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

    def test_classifies_profile_update_required_blocker(self) -> None:
        state = {
            "profile_update_required": True,
            "profile_update_url": "https://djinni.co/my/profile/",
            "profile_update_action_text": "Update profile",
        }

        self.assertEqual(
            classify_no_apply_state(state, {"ok": False, "reason": "no visible apply button"}),
            "profile update required before Djinni allows applying",
        )
        self.assertEqual(
            profile_update_blocker_fields(state),
            {
                "profile_update_required": True,
                "profile_update_url": "https://djinni.co/my/profile/",
                "profile_update_action_text": "Update profile",
            },
        )

    def test_profile_update_blocker_logs_explicit_profile_url_without_submit(self) -> None:
        class FakeTab:
            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "attempts.jsonl"
            profile_state = {
                "already_applied": False,
                "inactive": False,
                "profile_update_required": True,
                "profile_update_url": "",
                "applyish_controls": [],
            }
            profile_action = {
                "profile_update_required": True,
                "profile_update_url": "https://djinni.co/my/profile/",
                "profile_update_action_text": "Update profile",
                "profile_update_controls": [{"text": "Update profile", "href": "https://djinni.co/my/profile/"}],
            }

            with (
                patch("djinni_csv_apply.open_job_tab", return_value=FakeTab()),
                patch("djinni_csv_apply.wait_for_page", return_value=None),
                patch("djinni_csv_apply.inspect_state", return_value=profile_state),
                patch("djinni_csv_apply.open_apply_form", return_value={"ok": False, "reason": "no visible apply button"}),
                patch("djinni_csv_apply.discover_profile_update_action", return_value=profile_action),
                patch("djinni_csv_apply.fill_form", side_effect=AssertionError("should not fill form")),
                patch("djinni_csv_apply.submit_form", side_effect=AssertionError("should not submit")),
            ):
                rc = process_row(
                    make_row(),
                    endpoint="http://127.0.0.1:9222",
                    execute=True,
                    log_path=log_path,
                    delay=0,
                )

            self.assertEqual(rc, 3)
            event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(event["result"], "blocked_no_apply_form")
            self.assertEqual(event["blocked_reason"], "profile update required before Djinni allows applying")
            self.assertTrue(event["profile_update_required"])
            self.assertEqual(event["profile_update_url"], "https://djinni.co/my/profile/")
            self.assertEqual(event["profile_update_action_text"], "Update profile")
            self.assertEqual(event["state"]["profile_update_controls"][0]["href"], "https://djinni.co/my/profile/")


if __name__ == "__main__":
    unittest.main()
