from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import blocker_loop  # noqa: E402
from job_platforms import progress  # noqa: E402


class BlockerLoopTests(unittest.TestCase):
    def test_dou_sent_url_unconfirmed_result_is_resolved_inferred_success(self) -> None:
        event = {
            "site": "dou",
            "source_url": "https://jobs.dou.ua/companies/codetiburon/vacancies/360415/",
            "result": "submit_clicked_unconfirmed",
            "after": {
                "url": "https://jobs.dou.ua/companies/codetiburon/vacancies/360415/?sent#replied-id",
                "application_surfaces": [{"className": "replied sent "}],
            },
        }

        self.assertEqual(blocker_loop.normalize_result(event), "submitted_success_inferred")

    def test_profile_update_blocker_gets_manual_and_auto_options(self) -> None:
        event = {
            "site": "djinni",
            "source_url": "https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/",
            "company": "Visarsoft",
            "title": "Senior Python Backend Engineer",
            "result": "blocked_no_apply_form",
            "blocked_reason": "profile update required before Djinni allows applying",
            "profile_update_url": "https://djinni.co/my/profile/",
            "attempted_at": "2026-06-12T16:01:40+0300",
        }

        record = blocker_loop.build_blocker_record(event)

        self.assertEqual(record["category"], "profile_update_required")
        self.assertEqual(record["status"], "unresolved")
        labels = [option["label"] for option in record["resolution_options"]]
        self.assertIn("Manual: open Djinni profile update page", labels)
        self.assertIn("Auto: run bounded Djinni profile helper", labels)

    def test_djinni_eligibility_mismatch_is_warning_not_profile_update_error(self) -> None:
        event = {
            "site": "djinni",
            "source_url": "https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/",
            "company": "Visarsoft",
            "title": "Senior Python Backend Engineer",
            "result": "blocked_no_apply_form",
            "blocked_reason": "djinni eligibility mismatch: salary/location filters",
            "eligibility_mismatch": True,
            "attempted_at": "2026-06-19T15:34:37+0300",
        }

        record = blocker_loop.build_blocker_record(event)

        self.assertEqual(record["category"], "djinni_eligibility_mismatch")
        labels = [option["label"] for option in record["resolution_options"]]
        self.assertIn("Manual: skip this mismatched Djinni vacancy", labels)
        self.assertIn("Manual: lower salary or adjust locations, then retry", labels)
        self.assertNotIn("Auto: run bounded Djinni profile helper", labels)

    def test_progress_graph_includes_unresolved_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker_path = Path(tmp) / "unresolved_blockers.jsonl"
            blocker = blocker_loop.build_blocker_record(
                {
                    "site": "djinni",
                    "source_url": "https://djinni.co/jobs/1/",
                    "company": "Example",
                    "title": "Python Engineer",
                    "result": "blocked_no_apply_form",
                    "blocked_reason": "profile update required before Djinni allows applying",
                }
            )
            blocker_path.write_text(json.dumps(blocker, ensure_ascii=False) + "\n", encoding="utf-8")

            with patch.object(progress, "UNRESOLVED_BLOCKERS_PATH", blocker_path):
                snapshot = progress.build_progress_snapshot(Path(tmp) / "empty.sqlite3")

            blockers = [node for node in snapshot.nodes if node.kind == "blocker"]
            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0].status, "unresolved")
            self.assertEqual(blockers[0].data["category"], "profile_update_required")
            self.assertTrue(any(edge.source == blockers[0].id and edge.target == "stage:submit" for edge in snapshot.edges))

    def test_refresh_unresolved_blockers_reads_canonical_all_site_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            web_runs = base / "web_runs"
            web_runs.mkdir()
            dou_log = base / "dou_submission_attempts.jsonl"
            dou_log.write_text(
                json.dumps(
                    {
                        "site": "dou",
                        "source_url": "https://jobs.dou.ua/companies/example/vacancies/12345/",
                        "company": "Example",
                        "title": "Python Engineer",
                        "result": "blocked_validation",
                        "blocked_reason": "message quality validation failed",
                        "attempted_at": "2026-06-12T18:00:00+0300",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            blockers = blocker_loop.refresh_unresolved_blockers(
                web_runs,
                base / "unresolved_blockers.jsonl",
                log_paths={"dou": dou_log},
            )

            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["site"], "dou")
            self.assertEqual(blockers[0]["category"], "validation_or_approval_gate")

    def test_fill_failed_blocker_is_selector_or_fill_category(self) -> None:
        record = blocker_loop.build_blocker_record(
            {
                "site": "workua",
                "source_url": "https://www.work.ua/jobs/123/",
                "result": "blocked_fill_failed",
                "attempted_at": "2026-06-15T10:00:00+0300",
            }
        )

        self.assertEqual(record["category"], "fill_or_selector_failed")

    def test_prepare_success_clears_older_form_blocker_without_marking_submit_success(self) -> None:
        events = [
            {
                "site": "workua",
                "source_url": "https://www.work.ua/jobs/7768869/",
                "result": "blocked_fill_failed",
                "attempted_at": "2026-06-18T20:58:02+0300",
            },
            {
                "site": "workua",
                "source_url": "https://www.work.ua/jobs/7768869/",
                "result": "prepared_presubmit_ok",
                "attempted_at": "2026-06-19T15:27:30+0300",
            },
        ]

        blockers = blocker_loop.collect_unresolved_blockers(events)

        self.assertEqual(blockers, [])
        self.assertNotIn("prepared_presubmit_ok", blocker_loop.RESOLVED_RESULTS)

    def test_no_application_surface_blocker_is_site_changed_category(self) -> None:
        record = blocker_loop.build_blocker_record(
            {
                "site": "workua",
                "source_url": "https://www.work.ua/jobs/123/",
                "result": "blocked_no_application_surface",
                "attempted_at": "2026-06-15T10:00:00+0300",
            }
        )

        self.assertEqual(record["category"], "site_changed_or_no_apply_surface")

    def test_newer_specific_blocker_suppresses_stale_unknown_for_same_site_url(self) -> None:
        blockers = blocker_loop.collect_unresolved_blockers(
            [
                {
                    "site": "workua",
                    "source_url": "https://www.work.ua/jobs/123/",
                    "result": "blocked_unclassified",
                    "blocked_reason": "temporary blocker",
                    "attempted_at": "2026-06-15T10:00:00+0300",
                },
                {
                    "site": "workua",
                    "source_url": "https://www.work.ua/jobs/123/",
                    "result": "blocked_no_application_surface",
                    "attempted_at": "2026-06-15T10:05:00+0300",
                },
            ]
        )

        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["category"], "site_changed_or_no_apply_surface")
        self.assertEqual(blockers[0]["result"], "blocked_no_application_surface")

    def test_newer_eligibility_mismatch_suppresses_stale_profile_update_for_same_url(self) -> None:
        blockers = blocker_loop.collect_unresolved_blockers(
            [
                {
                    "site": "djinni",
                    "source_url": "https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/",
                    "result": "blocked_no_apply_form",
                    "blocked_reason": "profile update required before Djinni allows applying",
                    "attempted_at": "2026-06-19T15:34:42+0300",
                },
                {
                    "site": "djinni",
                    "source_url": "https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/",
                    "result": "blocked_no_apply_form",
                    "blocked_reason": "djinni eligibility mismatch: salary/location filters",
                    "eligibility_mismatch": True,
                    "attempted_at": "2026-06-19T15:55:00+0300",
                },
            ]
        )

        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["category"], "djinni_eligibility_mismatch")

    def test_newer_specific_blocker_suppresses_stale_unknown_when_old_site_missing(self) -> None:
        blockers = blocker_loop.collect_unresolved_blockers(
            [
                {
                    "source_url": "https://www.work.ua/jobs/123/",
                    "result": "blocked_unclassified",
                    "blocked_reason": "temporary blocker",
                    "attempted_at": "2026-06-15T10:00:00+0300",
                },
                {
                    "site": "workua",
                    "source_url": "https://www.work.ua/jobs/123/",
                    "result": "blocked_no_application_surface",
                    "attempted_at": "2026-06-15T10:05:00+0300",
                },
            ]
        )

        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["site"], "workua")
        self.assertEqual(blockers[0]["category"], "site_changed_or_no_apply_surface")

    def test_wrong_submitter_route_djinni_validation_is_suppressed(self) -> None:
        blockers = blocker_loop.collect_unresolved_blockers(
            [
                {
                    "site": "dou",
                    "source_url": "https://relocate.dou.ua/jobs/?category=Python",
                    "result": "blocked_validation",
                    "errors": [
                        "only site=djinni is supported by this script",
                        "url must be a Djinni job URL",
                    ],
                    "attempted_at": "2026-06-15T10:00:00+0300",
                },
                {
                    "site": "djinni_inbox",
                    "source_url": "https://djinni.co/my/inbox/25880123/#last",
                    "result": "blocked_validation",
                    "blocked_reason": "only site=djinni is supported by this script; url must be a Djinni job URL",
                    "attempted_at": "2026-06-15T10:01:00+0300",
                },
            ]
        )

        self.assertEqual(blockers, [])

    def test_real_validation_blocker_stays_unresolved(self) -> None:
        blockers = blocker_loop.collect_unresolved_blockers(
            [
                {
                    "site": "djinni",
                    "source_url": "https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/",
                    "result": "blocked_validation",
                    "errors": ["linkedin is required", "final_submit_allowed must be true for execute"],
                    "attempted_at": "2026-06-15T10:00:00+0300",
                }
            ]
        )

        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["site"], "djinni")
        self.assertEqual(blockers[0]["category"], "validation_or_approval_gate")
        self.assertIn("linkedin is required", blockers[0]["blocked_reason"])


if __name__ == "__main__":
    unittest.main()
