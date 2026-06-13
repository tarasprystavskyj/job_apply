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


if __name__ == "__main__":
    unittest.main()
