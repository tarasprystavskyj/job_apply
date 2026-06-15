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

import dou_csv_apply  # noqa: E402
from dou_csv_apply import DouApplicationRow  # noqa: E402


APPROVED_MESSAGE = (
    "Hi Example Team, I am interested in this Python automation role. "
    "My background matches the backend, API, and LLM workflow requirements."
)


def row(**overrides: object) -> DouApplicationRow:
    data = {
        "row_number": 2,
        "site": "dou",
        "url": "https://jobs.dou.ua/companies/example/vacancies/12345/",
        "title": "Python Engineer",
        "company": "Example",
        "message": APPROVED_MESSAGE,
        "linkedin": "",
        "linkedin_policy": "omit",
        "resume_policy": "no_resume",
        "approved_resume_name": "",
        "upload_allowed": False,
        "approved_to_submit": True,
        "final_submit_allowed": True,
        "answers": {},
    }
    data.update(overrides)
    return DouApplicationRow(**data)


class DouCsvApplyTest(unittest.TestCase):
    def test_validate_row_requires_dou_gate_data(self) -> None:
        errors = dou_csv_apply.validate_row(
            row(
                site="djinni",
                url="https://example.com/jobs/1",
                message="short",
                approved_to_submit=False,
                final_submit_allowed=False,
            )
        )
        self.assertIn("site must be dou", errors)
        self.assertIn("url must be an exact https DOU vacancy URL ending in /vacancies/<id>/", errors)
        self.assertIn("message looks too short for an approved application", errors)
        self.assertIn("approved_to_submit must be true", errors)
        self.assertIn("final_submit_allowed must be true", errors)

    def test_upload_policy_is_blocked_without_file_reads(self) -> None:
        errors = dou_csv_apply.validate_row(
            row(
                resume_policy="upload_file",
                upload_allowed=True,
                approved_resume_name="exact-approved.pdf",
            )
        )
        self.assertIn("resume file upload is not implemented; no resume files will be read or uploaded", errors)

    def test_csv_rows_reject_company_vacancies_page(self) -> None:
        errors = dou_csv_apply.validate_row(row(url="https://jobs.dou.ua/companies/example/vacancies/"))
        self.assertIn("url must be an exact https DOU vacancy URL ending in /vacancies/<id>/", errors)

    def test_exact_url_validation_accepts_jobs_and_relocate_vacancies(self) -> None:
        self.assertTrue(dou_csv_apply.is_exact_dou_vacancy_url("https://jobs.dou.ua/companies/example/vacancies/12345/"))
        self.assertTrue(dou_csv_apply.is_exact_dou_vacancy_url("https://relocate.dou.ua/companies/example/vacancies/12345/"))
        self.assertFalse(dou_csv_apply.is_exact_dou_vacancy_url("https://relocate.dou.ua/jobs/?category=Python&from=maybe"))

    def test_read_rows_rejects_message_file_for_live_dou_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "approved.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "site",
                        "url",
                        "message",
                        "message_file",
                        "linkedin_policy",
                        "resume_policy",
                        "upload_allowed",
                        "approved_to_submit",
                        "final_submit_allowed",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "site": "dou",
                        "url": "https://jobs.dou.ua/companies/example/vacancies/12345/",
                        "message": APPROVED_MESSAGE,
                        "message_file": "private-message.txt",
                        "linkedin_policy": "omit",
                        "resume_policy": "no_resume",
                        "upload_allowed": "false",
                        "approved_to_submit": "true",
                        "final_submit_allowed": "true",
                    }
                )

            with self.assertRaises(ValueError):
                dou_csv_apply.read_rows(csv_path)

    def test_dry_run_logs_approved_row_without_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "dou.jsonl"
            rc = dou_csv_apply.process_row(row(), "http://127.0.0.1:9222", "dry-run", log_path, 0)

            self.assertEqual(rc, 0)
            event = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(event["result"], "dry_run_ok")
            self.assertEqual(event["message_length"], len(APPROVED_MESSAGE))
            self.assertNotIn("message", event)

    def test_main_refuses_execute_without_explicit_final_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "approved.csv"
            csv_path.write_text(
                "site,url,message,linkedin_policy,resume_policy,upload_allowed,approved_to_submit,final_submit_allowed\n"
                f"dou,https://jobs.dou.ua/companies/example/vacancies/12345/,\"{APPROVED_MESSAGE}\",omit,no_resume,false,true,true\n",
                encoding="utf-8",
            )

            rc = dou_csv_apply.main(["--csv", str(csv_path), "--execute"])

            self.assertEqual(rc, 2)

    def test_prepare_stops_after_presubmit_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "dou.jsonl"
            fake_tab = Mock()
            with (
                patch.object(dou_csv_apply, "open_tab", return_value=fake_tab),
                patch.object(dou_csv_apply, "wait_for_page"),
                patch.object(dou_csv_apply, "inspect_state", return_value={"body_start": ""}),
                patch.object(dou_csv_apply, "open_application_surface", return_value={"ok": True, "opened": True}),
                patch.object(dou_csv_apply, "fill_form", return_value={"ok": True, "messageLength": len(APPROVED_MESSAGE)}),
                patch.object(dou_csv_apply, "validate_before_submit", return_value={"ok": True, "errors": [], "details": {}}),
                patch.object(dou_csv_apply, "submit_form") as submit_form,
            ):
                rc = dou_csv_apply.process_row(row(), "http://127.0.0.1:9222", "prepare", log_path, 0)

            self.assertEqual(rc, 0)
            submit_form.assert_not_called()
            event = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(event["result"], "pre_submit_validation_ok")

    def test_fill_form_treats_missing_linkedin_field_as_optional_skip(self) -> None:
        fake_tab = Mock()
        fake_tab.eval.return_value = {"ok": True}

        dou_csv_apply.fill_form(
            fake_tab,
            row(
                linkedin_policy="fill_url",
                linkedin="https://www.linkedin.com/in/taras-prystavskyj/",
            ),
        )

        expression = fake_tab.eval.call_args.args[0]
        self.assertIn('linkedinFilled = {skipped: true, reason: "no visible optional LinkedIn/profile field"}', expression)
        self.assertNotIn('return {ok: false, reason: "linkedin_policy=fill_url but no visible LinkedIn/profile field"}', expression)

    def test_presubmit_missing_linkedin_field_does_not_bypass_required_fields(self) -> None:
        fake_tab = Mock()
        fake_tab.eval.return_value = {"ok": True}

        dou_csv_apply.validate_before_submit(
            fake_tab,
            row(
                linkedin_policy="fill_url",
                linkedin="https://www.linkedin.com/in/taras-prystavskyj/",
            ),
        )

        expression = fake_tab.eval.call_args.args[0]
        self.assertIn("details.linkedinOptionalSkipped = true", expression)
        self.assertNotIn('errors.push("linkedin_policy=fill_url but no visible LinkedIn/profile field")', expression)
        self.assertIn("const requiredEmpty = Array.from(root.querySelectorAll", expression)
        self.assertIn('if (requiredEmpty.length) errors.push("visible required fields are empty")', expression)

    def test_execute_submits_only_after_presubmit_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "dou.jsonl"
            fake_tab = Mock()
            with (
                patch.object(dou_csv_apply, "open_tab", return_value=fake_tab),
                patch.object(dou_csv_apply, "wait_for_page"),
                patch.object(dou_csv_apply, "inspect_state", side_effect=[{"body_start": ""}, {"body_start": "thank you"}]),
                patch.object(dou_csv_apply, "open_application_surface", return_value={"ok": True, "opened": True}),
                patch.object(dou_csv_apply, "fill_form", return_value={"ok": True, "messageLength": len(APPROVED_MESSAGE)}),
                patch.object(dou_csv_apply, "validate_before_submit", return_value={"ok": True, "errors": [], "details": {}}),
                patch.object(dou_csv_apply, "submit_form", return_value={"ok": True, "submitted": True}) as submit_form,
            ):
                rc = dou_csv_apply.process_row(row(), "http://127.0.0.1:9222", "execute", log_path, 0)

            self.assertEqual(rc, 0)
            submit_form.assert_called_once()
            event = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(event["result"], "submitted_success")


if __name__ == "__main__":
    unittest.main()
