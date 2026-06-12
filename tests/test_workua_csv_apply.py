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

from workua_csv_apply import process_row, read_rows, validate_before_submit, validate_row  # noqa: E402


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
    "message_policy",
    "profile_resume_only_allowed",
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
        "message_policy": "exact_message",
        "profile_resume_only_allowed": "false",
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

    def test_profile_resume_only_requires_explicit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "profile_only.csv"
            write_csv(
                csv_path,
                [
                    approved_row(
                        resume_policy="use_workua_profile",
                        message_policy="profile_resume_only",
                        profile_resume_only_allowed="false",
                    )
                ],
            )
            row = read_rows(csv_path)[0]

            errors = validate_row(row, live_action=True)
            self.assertIn("profile_resume_only_allowed=true is required when message_policy=profile_resume_only", errors)

    def test_profile_resume_only_requires_profile_or_selected_resume_and_no_linkedin_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "profile_only_bad_policy.csv"
            write_csv(
                csv_path,
                [
                    approved_row(
                        resume_policy="no_resume",
                        linkedin_policy="fill_field",
                        linkedin="https://www.linkedin.com/in/example/",
                        message_policy="profile_resume_only",
                        profile_resume_only_allowed="true",
                    )
                ],
            )
            row = read_rows(csv_path)[0]

            errors = validate_row(row, live_action=True)
            self.assertIn("profile_resume_only requires linkedin_policy=no_linkedin because no message/linkedin field is filled", errors)
            self.assertIn("profile_resume_only requires resume_policy=use_workua_profile or use_selected_resume", errors)

    def test_profile_resume_only_gate_allows_workua_profile_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "profile_only_ok.csv"
            write_csv(
                csv_path,
                [
                    approved_row(
                        resume_policy="use_workua_profile",
                        message_policy="profile_resume_only",
                        profile_resume_only_allowed="true",
                    )
                ],
            )
            row = read_rows(csv_path)[0]

            self.assertEqual(validate_row(row, live_action=True), [])

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

    def test_process_row_profile_resume_only_prepare_skips_final_click(self) -> None:
        class FakeTab:
            closed = False

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "profile_only_ok.csv"
            log_path = Path(tmp) / "attempts.jsonl"
            write_csv(
                csv_path,
                [
                    approved_row(
                        resume_policy="use_workua_profile",
                        message_policy="profile_resume_only",
                        profile_resume_only_allowed="true",
                    )
                ],
            )
            row = read_rows(csv_path)[0]
            fake = FakeTab()

            with (
                patch("workua_csv_apply.open_tab", return_value=fake),
                patch("workua_csv_apply.wait_for_page", return_value=None),
                patch("workua_csv_apply.inspect_state", return_value={"already_applied": False}),
                patch("workua_csv_apply.navigate_to_apply_form", return_value={"ok": True, "openedReactModal": True}),
                patch(
                    "workua_csv_apply.fill_form",
                    return_value={"ok": True, "profileResumeOnly": True, "textareas": 0, "messageLength": 0},
                ),
                patch(
                    "workua_csv_apply.validate_before_submit",
                    return_value={
                        "ok": True,
                        "errors": [],
                        "details": {"profileResumeOnly": True, "messageSkippedByPolicy": True, "hasSubmitButton": True},
                    },
                ),
            ):
                rc = process_row(
                    row,
                    endpoint="http://127.0.0.1:1",
                    live_action=True,
                    execute=False,
                    log_path=log_path,
                    delay=0,
                )

            self.assertEqual(rc, 0)
            self.assertTrue(fake.closed)
            event = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(event["result"], "prepared_presubmit_ok")
            self.assertTrue(event["profile_resume_only_allowed"])
            self.assertEqual(event["message_policy"], "profile_resume_only")

    def test_validate_before_submit_script_contains_profile_resume_only_bypass(self) -> None:
        class CaptureTab:
            expression = ""

            def eval(self, expression: str) -> dict[str, object]:
                self.expression = expression
                return {"ok": True, "errors": [], "details": {}}

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "profile_only_ok.csv"
            write_csv(
                csv_path,
                [
                    approved_row(
                        resume_policy="use_workua_profile",
                        message_policy="profile_resume_only",
                        profile_resume_only_allowed="true",
                    )
                ],
            )
            row = read_rows(csv_path)[0]
            tab = CaptureTab()

            validate_before_submit(tab, row)  # type: ignore[arg-type]

            self.assertIn("messageSkippedByPolicy", tab.expression)
            self.assertIn("profile_resume_only", tab.expression)


if __name__ == "__main__":
    unittest.main()
