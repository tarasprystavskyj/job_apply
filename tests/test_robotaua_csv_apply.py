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


class FakeUploadTab:
    def __init__(self, expected_name: str) -> None:
        self.expected_name = expected_name
        self.calls: list[tuple[str, dict | None]] = []

    def call(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"result": {"objectId": "file-input-object"}}}
        if method == "DOM.setFileInputFiles":
            return {"result": {}}
        return {}

    def eval(self, expression: str) -> dict:
        if "attachSelector" in expression:
            return {"ok": True, "attachSelectorFound": True, "accept": ".pdf,.doc,.docx", "visibleInput": False}
        if "marked resume file input" in expression:
            return {"ok": True, "fileNames": [self.expected_name], "expectedName": self.expected_name}
        raise AssertionError("unexpected upload script")


class FakeVisibleUploadTab(FakeUploadTab):
    def eval(self, expression: str) -> dict:
        if "attachSelector" in expression:
            return {"ok": True, "attachSelectorFound": True, "accept": ".pdf,.doc,.docx", "visibleInput": False}
        if "marked resume file input" in expression:
            return {
                "ok": True,
                "fileNames": [],
                "fileNamesBeforeEvents": [],
                "visibleFileName": True,
                "expectedName": self.expected_name,
            }
        raise AssertionError("unexpected visible upload script")


class FakeWs:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.sent: list[dict] = []

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self) -> str:
        if not self.messages:
            raise AssertionError("unexpected websocket recv")
        return json.dumps(self.messages.pop(0))


class FakeChooserFallbackUploadTab(FakeUploadTab):
    def __init__(self, expected_name: str) -> None:
        super().__init__(expected_name)
        self._next_id = 100
        self.ws = FakeWs(
            [
                {"method": "Page.fileChooserOpened", "params": {"backendNodeId": 777}},
                {"id": 100, "result": {"result": {"value": {"ok": True}}}},
            ]
        )
        self.verify_calls = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"result": {"objectId": "file-input-object"}}}
        if method == "DOM.setFileInputFiles":
            return {"result": {}}
        if method == "DOM.getDocument":
            return {"result": {}}
        if method == "Page.setInterceptFileChooserDialog":
            return {"result": {}}
        return {}

    def eval(self, expression: str) -> dict:
        if "attachSelector" in expression:
            return {"ok": True, "attachSelectorFound": True, "accept": ".pdf,.doc,.docx", "visibleInput": False}
        if "marked resume file input" in expression or "resume file input disappeared" in expression:
            self.verify_calls += 1
            if self.verify_calls == 1:
                return {"ok": False, "fileNames": [], "expectedName": self.expected_name}
            return {"ok": True, "fileNames": [self.expected_name], "expectedName": self.expected_name}
        raise AssertionError("unexpected chooser fallback script")


class FakePolicyRequiresResumeTab:
    def __init__(self) -> None:
        self.closed = False

    def call(self, _method: str, _params: dict | None = None) -> dict:
        return {}

    def close(self) -> None:
        self.closed = True

    def eval(self, expression: str) -> dict:
        if "already_applied" in expression:
            return {
                "url": "https://robota.ua/company123/vacancy987654/apply?newApply=true",
                "title": "Apply",
                "body_start": (
                    "\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0441\u043f\u043e\u0441\u0456\u0431 "
                    "\u0432\u0456\u0434\u0433\u0443\u043a\u0443 / "
                    "\u0421\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u0440\u0435\u0437\u044e\u043c\u0435 / "
                    "\u041f\u0440\u0438\u043a\u0440\u0456\u043f\u0438\u0442\u0438 \u0444\u0430\u0439\u043b "
                    "\u0437 \u0440\u0435\u0437\u044e\u043c\u0435 / "
                    "\u0414\u043e\u0434\u0430\u0442\u0438 \u0441\u0443\u043f\u0440\u043e\u0432\u0456\u0434\u043d\u0438\u0439 "
                    "\u043b\u0438\u0441\u0442 / \u041f\u0440\u043e\u0434\u043e\u0432\u0436\u0438\u0442\u0438"
                ),
                "already_applied": False,
                "login_required": False,
                "inactive": False,
                "controls": [],
                "fields": [],
                "alerts": [],
            }
        raise AssertionError("unexpected script")


class FakeApplyMethodTab:
    def eval(self, expression: str) -> dict:
        if "attach-resume method selected" in expression:
            return {"ok": True, "opened": True, "reason": "attach-resume method selected", "text": "Attach"}
        raise AssertionError("unexpected apply-method script")


class FakeCoverLetterTab:
    def eval(self, expression: str) -> dict:
        if "cover-letter opener" in expression:
            return {"ok": True, "opened": True, "text": "Add cover letter"}
        raise AssertionError("unexpected cover-letter script")


class FakeExactCoverLetterTab:
    def eval(self, expression: str) -> dict:
        if apply.ROBOTAU_COVER_LETTER_SELECTOR not in expression:
            raise AssertionError("exact cover letter selector was not used")
        return {"ok": True, "opened": True, "selector": apply.ROBOTAU_COVER_LETTER_SELECTOR}


class FakeExactSubmitTab:
    def eval(self, expression: str) -> dict:
        if apply.ROBOTAU_FINAL_SEND_SELECTOR not in expression:
            raise AssertionError("exact final send selector was not used")
        return {"ok": True, "submitted": True, "selector": apply.ROBOTAU_FINAL_SEND_SELECTOR}


class FakeFillCoverSelectorTab:
    def eval(self, expression: str) -> dict:
        if "filledAnswers" not in expression:
            raise AssertionError("unexpected fill script")
        if apply.ROBOTAU_COVER_LETTER_SELECTOR not in expression:
            raise AssertionError("exact cover letter selector was not used during fill")
        return {
            "ok": True,
            "messageLength": len(EXACT_MESSAGE),
            "linkedinFields": 1,
            "linkedin": "https://www.linkedin.com/in/taras-prystavskyj/",
            "fileInputs": 0,
            "selectedResumeText": [],
            "coverLetterSelectorUsed": True,
        }


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

    def test_validate_row_accepts_exact_resume_file_inside_resume_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, "DEFAULT_RESUME_DIR", Path(tmp)):
            resume = Path(tmp) / "Senior Python CV.pdf"
            resume.write_bytes(b"")
            row = approved_row(
                resume_policy="upload_exact_resume",
                approved_resume_name=resume.name,
                upload_allowed=True,
            )
            self.assertEqual(apply.validate_row(row, "pre_submit"), [])

    def test_validate_row_blocks_resume_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, "DEFAULT_RESUME_DIR", Path(tmp)):
            row = approved_row(
                resume_policy="upload_exact_resume",
                approved_resume_name="..\\Senior Python CV.pdf",
                upload_allowed=True,
            )
            errors = apply.validate_row(row, "pre_submit")
            self.assertIn("approved_resume_name must be an exact file name, not a path", errors)

    def test_validate_row_blocks_approved_resume_path_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, "DEFAULT_RESUME_DIR", Path(tmp) / "resumes"):
            outside = Path(tmp) / "outside" / "Senior Python CV.pdf"
            row = approved_row(
                resume_policy="upload_exact_resume",
                approved_resume_name=outside.name,
                approved_resume_path=str(outside),
                upload_allowed=True,
            )
            errors = apply.validate_row(row, "pre_submit")
            self.assertIn("approved resume path must stay inside the resume directory or approved artifact directory", errors)

    def test_explicit_approved_resume_path_does_not_fall_back_to_same_name_default_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resume_dir = Path(tmp) / "resumes"
            artifact_dir = Path(tmp) / "artifacts"
            resume_dir.mkdir()
            artifact_dir.mkdir()
            fallback = resume_dir / "Senior Python CV.pdf"
            fallback.write_bytes(b"")
            explicit_missing = artifact_dir / fallback.name
            row = approved_row(
                resume_policy="upload_exact_resume",
                approved_resume_name=fallback.name,
                approved_resume_path=str(explicit_missing),
                upload_allowed=True,
            )
            with (
                patch.object(apply, "DEFAULT_RESUME_DIR", resume_dir),
                patch.object(apply, "APPROVED_RESUME_ARTIFACT_DIRS", (artifact_dir,)),
            ):
                errors = apply.validate_row(row, "pre_submit")
            self.assertIn(f"approved resume file was not found: {explicit_missing}", errors)

    def test_validate_row_accepts_approved_no_resume_submit_row(self) -> None:
        self.assertEqual(apply.validate_row(approved_row(), "submit"), [])

    def test_application_form_url_adds_new_apply_path(self) -> None:
        self.assertEqual(
            apply.application_form_url("https://robota.ua/company3685368/vacancy11052703"),
            "https://robota.ua/company3685368/vacancy11052703/apply?newApply=true",
        )
        self.assertEqual(
            apply.application_form_url("https://robota.ua/company3685368/vacancy11052703/apply?newApply=true"),
            "https://robota.ua/company3685368/vacancy11052703/apply?newApply=true",
        )
        self.assertEqual(
            apply.application_form_url("https://robota.ua/company3685368/vacancy11052703?from=search"),
            "https://robota.ua/company3685368/vacancy11052703/apply?from=search&newApply=true",
        )

    def test_attach_resume_file_uses_cdp_without_reading_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, "DEFAULT_RESUME_DIR", Path(tmp)):
            resume = Path(tmp) / "Senior Python CV.pdf"
            resume.write_bytes(b"")
            row = approved_row(
                resume_policy="upload_exact_resume",
                approved_resume_name=resume.name,
                upload_allowed=True,
            )
            fake = FakeUploadTab(resume.name)
            result = apply.attach_resume_file(fake, row)
            self.assertTrue(result["ok"])
            self.assertIn(("DOM.setFileInputFiles", {"objectId": "file-input-object", "files": [str(resume)]}), fake.calls)

    def test_attach_resume_file_accepts_visible_filename_even_when_input_files_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, "DEFAULT_RESUME_DIR", Path(tmp)):
            resume = Path(tmp) / "Senior Python CV.pdf"
            resume.write_bytes(b"")
            row = approved_row(
                resume_policy="upload_exact_resume",
                approved_resume_name=resume.name,
                upload_allowed=True,
            )
            fake = FakeVisibleUploadTab(resume.name)

            result = apply.attach_resume_file(fake, row)

            self.assertTrue(result["ok"])
            self.assertTrue(result["verify"]["visibleFileName"])

    def test_attach_resume_file_falls_back_to_file_chooser_when_direct_set_does_not_stick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply, "DEFAULT_RESUME_DIR", Path(tmp)):
            resume = Path(tmp) / "Senior Python CV.pdf"
            resume.write_bytes(b"")
            row = approved_row(
                resume_policy="upload_exact_resume",
                approved_resume_name=resume.name,
                upload_allowed=True,
            )
            fake = FakeChooserFallbackUploadTab(resume.name)

            result = apply.attach_resume_file(fake, row)

            self.assertTrue(result["ok"])
            self.assertTrue(result["chooser"]["ok"])
            self.assertIn(("DOM.setFileInputFiles", {"backendNodeId": 777, "files": [str(resume)]}), fake.calls)

    def test_no_resume_policy_blocks_when_apply_page_requires_resume_method(self) -> None:
        fake = FakePolicyRequiresResumeTab()
        with tempfile.TemporaryDirectory() as tmp, patch.object(apply.time, "sleep", lambda _seconds: None):
            log = Path(tmp) / "attempts.jsonl"
            rc = apply.process_row(
                approved_row(resume_policy="no_resume", approved_resume_name="", upload_allowed=False),
                "http://127.0.0.1:9222",
                "pre_submit",
                log,
                0,
                tab_factory=lambda _endpoint, _url: fake,
            )
            self.assertEqual(rc, 3)
            self.assertTrue(fake.closed)
            event = json.loads(log.read_text(encoding="utf-8").strip())
            self.assertEqual(event["result"], "blocked_policy_requires_resume")

    def test_upload_policy_can_select_attach_resume_apply_method(self) -> None:
        row = approved_row(resume_policy="upload_exact_resume", approved_resume_name="Senior Python CV.pdf", upload_allowed=True)
        result = apply.open_application_form(FakeApplyMethodTab(), row)
        self.assertEqual(result["reason"], "attach-resume method selected")

    def test_cover_letter_opener_is_detected_before_fill(self) -> None:
        result = apply.open_cover_letter(FakeCoverLetterTab())
        self.assertTrue(result["ok"])
        self.assertTrue(result["opened"])

    def test_exact_cover_letter_selector_is_used_first(self) -> None:
        result = apply.open_cover_letter(FakeExactCoverLetterTab())
        self.assertEqual(result["selector"], apply.ROBOTAU_COVER_LETTER_SELECTOR)

    def test_fill_form_uses_exact_cover_letter_selector(self) -> None:
        result = apply.fill_form(FakeFillCoverSelectorTab(), approved_row())
        self.assertTrue(result["ok"])
        self.assertTrue(result["coverLetterSelectorUsed"])

    def test_final_send_uses_exact_selector_first(self) -> None:
        result = apply.submit_form(FakeExactSubmitTab())
        self.assertTrue(result["submitted"])
        self.assertEqual(result["selector"], apply.ROBOTAU_FINAL_SEND_SELECTOR)

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
