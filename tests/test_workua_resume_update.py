from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import workua_resume_update as update  # noqa: E402


def config(**overrides: object) -> update.ResumeUpdateConfig:
    base = {
        "row_number": 1,
        "site": "workua",
        "resume_edit_url": "https://www.work.ua/jobseeker/my/resumes/edit/?id=123456",
        "field_updates": (
            update.FieldUpdate(
                key="title",
                selector="#resume-title",
                value="Senior Python Engineer",
            ),
        ),
        "approved_to_prepare": True,
        "final_save_allowed": True,
        "resume_file_policy": "no_upload",
        "upload_allowed": False,
        "approved_resume_name": "",
        "approved_resume_path": "",
    }
    base.update(overrides)
    return update.ResumeUpdateConfig(**base)


class WorkUaResumeUpdateTest(unittest.TestCase):
    def test_validates_exact_workua_resume_edit_url(self) -> None:
        self.assertTrue(update.is_workua_resume_edit_url("https://www.work.ua/jobseeker/my/resumes/edit/?id=3508069"))
        self.assertTrue(update.is_workua_resume_edit_url("https://work.ua/jobseeker/my/resumes/edit/?id=1"))
        self.assertFalse(update.is_workua_resume_edit_url("http://www.work.ua/jobseeker/my/resumes/edit/?id=3508069"))
        self.assertFalse(update.is_workua_resume_edit_url("https://www.work.ua/jobseeker/my/resumes/edit/"))
        self.assertFalse(update.is_workua_resume_edit_url("https://www.work.ua/jobs/8170878/"))
        self.assertFalse(update.is_workua_resume_edit_url("https://example.com/jobseeker/my/resumes/edit/?id=3508069"))

    def test_live_prepare_and_save_require_row_gates(self) -> None:
        cfg = config(approved_to_prepare=False, final_save_allowed=False)

        self.assertEqual(update.validate_config(cfg, live_action=False, execute_save=False), [])
        prepare_errors = update.validate_config(cfg, live_action=True, execute_save=False)
        self.assertIn("approved_to_prepare must be true for browser prepare/save", prepare_errors)
        save_errors = update.validate_config(cfg, live_action=True, execute_save=True)
        self.assertIn("approved_to_prepare must be true for browser prepare/save", save_errors)
        self.assertIn("final_save_allowed must be true for final save", save_errors)

    def test_field_updates_are_generic_and_not_hardcoded_to_owner(self) -> None:
        raw = {
            "site": "workua",
            "resume_edit_url": "https://www.work.ua/jobseeker/my/resumes/edit/?id=777",
            "field_updates": [
                {
                    "key": "summary",
                    "name": "summary",
                    "kind": "textarea",
                    "value": "Generic approved summary.",
                },
                {
                    "key": "city",
                    "label": "City",
                    "kind": "text",
                    "value": "Lviv",
                },
            ],
            "approved_to_prepare": "true",
            "final_save_allowed": "false",
        }

        cfg = update.config_from_mapping(raw)

        self.assertEqual(cfg.resume_edit_url, "https://www.work.ua/jobseeker/my/resumes/edit/?id=777")
        self.assertEqual([field.key for field in cfg.field_updates], ["summary", "city"])
        self.assertTrue(cfg.approved_to_prepare)
        self.assertFalse(cfg.final_save_allowed)
        self.assertEqual(update.validate_config(cfg, live_action=True, execute_save=False), [])

    def test_upload_exact_file_requires_exact_path_name_and_gate(self) -> None:
        cfg = config(
            resume_file_policy="upload_exact_file",
            upload_allowed=True,
            approved_resume_name="candidate.pdf",
            approved_resume_path=r"C:\tmp\other.pdf",
        )

        errors = update.validate_config(cfg, live_action=True, execute_save=False)

        self.assertIn("approved_resume_path basename must match approved_resume_name", errors)

    def test_upload_exact_file_rejects_path_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(update, "DEFAULT_RESUME_DIR", Path(tmp) / "resumes"):
            outside = Path(tmp) / "outside" / "candidate.pdf"
            cfg = config(
                resume_file_policy="upload_exact_file",
                upload_allowed=True,
                approved_resume_name=outside.name,
                approved_resume_path=str(outside),
            )

            errors = update.validate_config(cfg, live_action=True, execute_save=False)

            self.assertIn("approved resume path must stay inside the resume directory or approved artifact directory", errors)

    def test_upload_exact_file_accepts_existing_file_inside_resume_dir_without_reading_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(update, "DEFAULT_RESUME_DIR", Path(tmp)):
            resume = Path(tmp) / "candidate.pdf"
            resume.write_bytes(b"")
            cfg = config(
                resume_file_policy="upload_exact_file",
                upload_allowed=True,
                approved_resume_name=resume.name,
                approved_resume_path=str(resume),
            )

            self.assertEqual(update.validate_config(cfg, live_action=True, execute_save=False), [])

    def test_process_config_default_dry_run_does_not_open_browser_or_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "resume_update.jsonl"
            cfg = config(approved_to_prepare=False, final_save_allowed=False)

            with patch.object(update, "open_tab", side_effect=AssertionError("browser should not open")):
                rc = update.process_config(
                    cfg,
                    endpoint="http://127.0.0.1:1",
                    live_action=False,
                    execute_save=False,
                    log_path=log_path,
                    delay=0,
                )

            self.assertEqual(rc, 0)
            event = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(event["schema"], "job.workua_resume_update_attempt.v0")
            self.assertEqual(event["result"], "dry_run_ok")
            self.assertFalse(event["browser_prepare"])
            self.assertFalse(event["execute_save"])
            self.assertEqual(event["field_updates"][0]["value_length"], len("Senior Python Engineer"))
            self.assertNotIn("Senior Python Engineer", log_path.read_text(encoding="utf-8"))

    def test_main_refuses_execute_guard_before_reading_missing_json(self) -> None:
        missing = str(Path(tempfile.gettempdir()) / "missing-workua-resume-update.json")

        rc = update.main(["--json", missing, "--execute-save"])

        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
