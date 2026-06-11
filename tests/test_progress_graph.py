from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_apply_web import event_summary  # noqa: E402
from job_platforms.models import OutreachDraft, VacancyObservation  # noqa: E402
from job_platforms.progress import build_progress_snapshot  # noqa: E402
from shared_job_db import add_outreach_draft, update_outreach_approval, upsert_job  # noqa: E402


class SharedProgressGraphTest(unittest.TestCase):
    def test_submit_gate_waits_for_final_submit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.sqlite3"
            job_id = upsert_job(
                VacancyObservation(
                    source_site="workua",
                    source_url="https://www.work.ua/jobs/1/",
                    title="Python Engineer",
                    company="Example",
                    status="observed",
                    fit_tags=("python",),
                ),
                score=80,
                db_path=db_path,
            )
            outreach_id = add_outreach_draft(
                job_id,
                OutreachDraft(
                    source_site="workua",
                    source_url="https://www.work.ua/jobs/1/",
                    company="Example",
                    title="Python Engineer",
                    cover_letter="Draft only.",
                ),
                db_path=db_path,
            )

            pending = build_progress_snapshot(db_path)
            status_by_id = {node.id: node.status for node in pending.nodes}
            self.assertEqual(status_by_id["stage:review"], "active")
            self.assertEqual(status_by_id["stage:submit"], "pending")

            update_outreach_approval(
                outreach_id,
                "approved_to_submit",
                approved_to_prepare=True,
                approved_to_submit=True,
                final_submit_allowed=True,
                db_path=db_path,
            )

            ready = build_progress_snapshot(db_path)
            status_by_id = {node.id: node.status for node in ready.nodes}
            self.assertEqual(status_by_id["stage:submit"], "ready")

    def test_event_summary_uses_status_and_payload_message(self) -> None:
        self.assertEqual(
            event_summary({"status": "blocked", "payload_json": '{"message": "profile update required"}'}),
            "blocked: profile update required",
        )
        self.assertEqual(event_summary({"status": "observed", "payload_json": "{}"}), "observed")


if __name__ == "__main__":
    unittest.main()
