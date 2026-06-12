from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_platforms.models import DiscoveryQuery  # noqa: E402
from job_platforms.orchestrator import OrchestratorConfig, PublicHtmlInput, build_parser, run_once  # noqa: E402
from shared_job_db import fetch_progress_rows  # noqa: E402


PUBLIC_WORKUA_HTML = """
<!doctype html>
<html>
<head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Senior Python AI Automation Engineer",
    "url": "https://www.work.ua/jobs/7745352/",
    "hiringOrganization": {"@type": "Organization", "name": "Example Work Tech"},
    "jobLocation": {"@type": "Place", "address": {"addressLocality": "Lviv", "addressCountry": "UA"}},
    "description": "Build Python services, LLM workflows, FastAPI APIs, and automation.",
    "datePosted": "2026-06-10",
    "employmentType": "FULL_TIME"
  }
  </script>
</head>
<body>
  <a href="/jobs/7750000/">Python Backend Engineer</a>
</body>
</html>
"""


class PlatformOrchestratorTest(unittest.TestCase):
    def test_run_once_plans_discovery_and_persists_review_only_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            html_path = base / "workua.html"
            db_path = base / "shared.sqlite3"
            snapshot_path = base / "progress.json"
            html_path.write_text(PUBLIC_WORKUA_HTML, encoding="utf-8")

            result = run_once(
                OrchestratorConfig(
                    query=DiscoveryQuery(text="Python AI", limit=10),
                    platform_ids=("workua",),
                    public_html=(
                        PublicHtmlInput(
                            platform_id="workua",
                            path=html_path,
                            source_url="https://www.work.ua/jobs-Python+AI/",
                        ),
                    ),
                    db_path=db_path,
                    limit=10,
                    snapshot_out=snapshot_path,
                )
            )

            self.assertEqual(result.discovery_urls["workua"], ["https://www.work.ua/jobs-Python+AI/"])
            self.assertEqual(result.observed_jobs, 2)
            self.assertEqual(result.drafts, 2)
            self.assertEqual(result.blockers, [])
            self.assertTrue(snapshot_path.exists())

            rows = fetch_progress_rows(db_path)
            self.assertEqual(len(rows["jobs"]), 2)
            self.assertEqual(len(rows["outreach"]), 2)
            self.assertTrue(all(not row["submission_allowed"] for row in rows["outreach"]))
            self.assertTrue(all(not row["upload_allowed"] for row in rows["outreach"]))
            self.assertTrue(any(event["event_type"] == "platform_discovery_planned" for event in rows["events"]))

    def test_run_once_can_ingest_public_raw_json_without_creating_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw_path = base / "vacancies.json"
            db_path = base / "shared.sqlite3"
            raw_path.write_text(
                json.dumps(
                    [
                        {
                            "platform": "workua",
                            "source_url": "https://www.work.ua/jobs/1/",
                            "title": "Python Engineer",
                            "company": "Example",
                            "summary": "Python backend automation",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            result = run_once(
                OrchestratorConfig(
                    query=DiscoveryQuery(text="Python"),
                    platform_ids=("workua",),
                    raw_vacancies_path=raw_path,
                    db_path=db_path,
                    draft=False,
                )
            )

            self.assertEqual(result.observed_jobs, 1)
            self.assertEqual(result.drafts, 0)
            rows = fetch_progress_rows(db_path)
            self.assertEqual(len(rows["jobs"]), 1)
            self.assertEqual(rows["outreach"], [])

    def test_help_includes_examples_and_run_once_default(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("Examples:", help_text)
        self.assertIn("platform_loop.py --once", help_text)
        self.assertIn("Run one safe GREEN platform orchestration pass", help_text)


if __name__ == "__main__":
    unittest.main()
