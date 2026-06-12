from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_platforms import ApprovalGate, DiscoveryQuery  # noqa: E402
from job_platforms.base import UnsupportedAction  # noqa: E402
from job_platforms.robotaua import (  # noqa: E402
    RobotaUaAdapter,
    build_robotaua_progress_snapshot,
    create_robotaua_review_draft,
    persist_robotaua_observation,
    snapshot_to_dict,
)
from robotaua_pipeline import build_progress_snapshot, build_review_queue, normalize_html, run_once  # noqa: E402


PUBLIC_LISTING_HTML = """
<!doctype html>
<html>
<head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Senior Python AI Automation Engineer",
    "url": "https://robota.ua/company123/vacancy987654",
    "hiringOrganization": {"@type": "Organization", "name": "Example Tech"},
    "jobLocation": {"@type": "Place", "address": {"addressLocality": "Lviv", "addressCountry": "UA"}},
    "description": "Build Python services, LLM workflows, FastAPI integrations, and automation.",
    "datePosted": "2026-06-10",
    "employmentType": "FULL_TIME"
  }
  </script>
</head>
<body>
  <a href="/company456/vacancy111222">Python Backend Engineer</a>
</body>
</html>
"""


class RobotaUaAdapterTest(unittest.TestCase):
    def test_discovery_url_is_public_search(self) -> None:
        adapter = RobotaUaAdapter()
        urls = adapter.discovery_urls(DiscoveryQuery(text="Python AI"))
        self.assertEqual(urls, ["https://robota.ua/zapros/python-ai/ukraine"])

    def test_extract_public_json_ld_and_anchor_vacancies(self) -> None:
        adapter = RobotaUaAdapter()
        observations = adapter.extract_public_vacancies(
            PUBLIC_LISTING_HTML,
            "https://robota.ua/zapros/python-ai/ukraine",
            DiscoveryQuery(text="Python AI"),
        )
        self.assertEqual(len(observations), 2)
        first = observations[0]
        self.assertEqual(first.source_site, "robotaua")
        self.assertEqual(first.company, "Example Tech")
        self.assertIn("python", first.fit_tags)
        self.assertIn("ai", first.fit_tags)
        self.assertIn("manual_handoff_only", first.risk_flags)
        self.assertFalse(first.source_url.endswith("?"))

    def test_prepare_and_submit_are_not_implemented(self) -> None:
        adapter = RobotaUaAdapter()
        vacancy = adapter.normalize_vacancy(
            {
                "source_url": "https://robota.ua/company123/vacancy987654",
                "title": "Python Engineer",
            }
        )
        draft = adapter.draft_outreach(vacancy)
        with self.assertRaises(PermissionError):
            adapter.prepare_application(draft, ApprovalGate())
        with self.assertRaises(UnsupportedAction):
            adapter.prepare_application(draft, ApprovalGate(approved_to_prepare=True))
        with self.assertRaises(PermissionError):
            adapter.final_submit(draft, ApprovalGate(approved_to_submit=True, final_submit_allowed=False))

    def test_pipeline_outputs_review_only_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            html_path = base / "listing.html"
            observations_path = base / "robotaua_observations.jsonl"
            drafts_path = base / "robotaua_outreach_drafts.jsonl"
            review_csv = base / "robotaua_review.csv"
            progress_path = base / "robotaua_progress.json"
            status_path = base / "robotaua_status.jsonl"
            db_path = base / "shared.sqlite3"
            html_path.write_text(PUBLIC_LISTING_HTML, encoding="utf-8")

            result = normalize_html(
                html_path,
                "https://robota.ua/zapros/python-ai/ukraine",
                "Python AI",
                observations_path,
                db_path=db_path,
                status_path=status_path,
            )
            self.assertEqual(result["observations"], 2)

            review = build_review_queue(
                observations_path,
                drafts_path,
                review_csv,
                progress_path,
                limit=5,
                db_path=db_path,
                status_path=status_path,
            )
            self.assertEqual(review["rows"], 2)
            with review_csv.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["site"], "robotaua")
            self.assertEqual(rows[0]["approved_to_submit"], "false")
            self.assertEqual(rows[0]["final_submit_allowed"], "false")
            self.assertEqual(rows[0]["manual_handoff_required"], "true")

            draft_rows = [json.loads(line) for line in drafts_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(draft_rows[0]["schema"], "job.application_draft.v0")
            self.assertFalse(draft_rows[0]["submission_allowed"])
            self.assertFalse(draft_rows[0]["upload_allowed"])

            snapshot = build_progress_snapshot(observations_path, drafts_path, base / "empty_status.jsonl")
            self.assertEqual(snapshot.schema, "job.progress_snapshot.langgraph.v0")
            status_by_id = {node.id: node.status for node in snapshot.nodes}
            self.assertEqual(status_by_id["review_approval"], "blocked_waiting_owner")
            self.assertEqual(status_by_id["manual_handoff"], "blocked_waiting_owner")

    def test_shared_db_review_draft_and_progress_graph(self) -> None:
        adapter = RobotaUaAdapter()
        vacancy = adapter.extract_public_vacancies(
            PUBLIC_LISTING_HTML,
            "https://robota.ua/zapros/python-ai/ukraine",
            DiscoveryQuery(text="Python AI"),
        )[0]

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "robotaua.sqlite3"
            job_id = persist_robotaua_observation(vacancy, db_path=db_path)
            outreach_id = create_robotaua_review_draft(vacancy, job_id, db_path=db_path)
            self.assertGreater(job_id, 0)
            self.assertGreater(outreach_id, 0)

            payload = snapshot_to_dict(build_robotaua_progress_snapshot(db_path=db_path))
            self.assertEqual(payload["schema"], "job.robotaua_progress_snapshot.langgraph.v0")
            status_by_id = {node["id"]: node["status"] for node in payload["nodes"]}
            self.assertEqual(status_by_id["robotaua:normalize"], "complete")
            self.assertEqual(status_by_id["robotaua:score"], "complete")
            self.assertEqual(status_by_id["robotaua:draft"], "complete")
            self.assertEqual(status_by_id["robotaua:review_approval"], "blocked_waiting_owner")
            self.assertEqual(status_by_id["robotaua:final_gated_apply_manual_handoff"], "blocked_waiting_owner")

            handoff_events = [
                row for row in payload["event_tail"] if row.get("event_type") == "robotaua_manual_handoff_required"
            ]
            self.assertTrue(handoff_events)
            self.assertIn("approval_request_text", handoff_events[-1]["payload"])

    def test_run_once_uses_public_fetch_and_stops_at_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db_path = base / "shared.sqlite3"
            original_fetch = __import__("robotaua_pipeline").fetch_public_search_page
            try:
                __import__("robotaua_pipeline").fetch_public_search_page = lambda _url: PUBLIC_LISTING_HTML
                result = run_once(
                    "Python AI",
                    "",
                    5,
                    1,
                    0,
                    base / "observations.jsonl",
                    base / "drafts.jsonl",
                    base / "review.csv",
                    base / "progress.json",
                    base / "pages",
                    db_path=db_path,
                    status_path=base / "status.jsonl",
                    blocker_path=base / "blockers.jsonl",
                )
            finally:
                __import__("robotaua_pipeline").fetch_public_search_page = original_fetch

            self.assertEqual(result["fetched_pages"], 1)
            self.assertEqual(result["observations"], 2)
            self.assertEqual(result["review_rows"], 2)
            draft_rows = [json.loads(line) for line in (base / "drafts.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertFalse(draft_rows[0]["submission_allowed"])
            self.assertFalse(draft_rows[0]["upload_allowed"])
            self.assertIn("approval_request_text", draft_rows[0])


if __name__ == "__main__":
    unittest.main()
