from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_platforms import ApprovalGate, DiscoveryQuery  # noqa: E402
from job_platforms.base import UnsupportedAction  # noqa: E402
from job_platforms.workua import (  # noqa: E402
    WorkUaAdapter,
    build_workua_progress_snapshot,
    create_workua_review_draft,
    persist_workua_observation,
    pipeline_gate_data,
    run_workua_public_search_once,
    snapshot_to_dict,
)


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
  <a href="/jobs/7745352/">Senior Python AI Automation Engineer duplicate</a>
  <a href="/jobs/7750000/">Python Backend Engineer</a>
  <a href="/jobs-python/">Search page, not a vacancy</a>
</body>
</html>
"""


class WorkUaAdapterTest(unittest.TestCase):
    def test_discovery_url_is_public_search(self) -> None:
        adapter = WorkUaAdapter()
        urls = adapter.discovery_urls(DiscoveryQuery(text="Python AI"))
        self.assertEqual(urls, ["https://www.work.ua/jobs-Python+AI/"])

    def test_extract_public_json_ld_and_anchor_vacancies(self) -> None:
        adapter = WorkUaAdapter()
        observations = adapter.extract_public_vacancies(
            PUBLIC_WORKUA_HTML,
            "https://www.work.ua/jobs-Python+AI/",
            DiscoveryQuery(text="Python AI"),
        )
        self.assertEqual(len(observations), 2)
        first = observations[0]
        self.assertEqual(first.source_site, "workua")
        self.assertEqual(first.company, "Example Work Tech")
        self.assertEqual(first.raw["workua_vacancy_id"], "7745352")
        self.assertIn("python", first.fit_tags)
        self.assertIn("ai", first.fit_tags)
        self.assertIn("manual_handoff_only", first.risk_flags)
        self.assertTrue(first.source_url.endswith("/jobs/7745352/"))

    def test_prepare_and_submit_remain_fail_closed(self) -> None:
        adapter = WorkUaAdapter()
        vacancy = adapter.normalize_vacancy(
            {
                "source_url": "https://www.work.ua/jobs/7745352/",
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

    def test_shared_db_review_draft_and_progress_graph(self) -> None:
        adapter = WorkUaAdapter()
        vacancy = adapter.extract_public_vacancies(
            PUBLIC_WORKUA_HTML,
            "https://www.work.ua/jobs-Python+AI/",
            DiscoveryQuery(text="Python AI"),
        )[0]

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "workua.sqlite3"
            job_id = persist_workua_observation(vacancy, db_path=db_path)
            outreach_id = create_workua_review_draft(vacancy, job_id, db_path=db_path)
            self.assertGreater(job_id, 0)
            self.assertGreater(outreach_id, 0)

            snapshot = build_workua_progress_snapshot(db_path=db_path)
            payload = snapshot_to_dict(snapshot)
            self.assertEqual(payload["schema"], "job.workua_progress_snapshot.langgraph.v0")
            status_by_id = {node["id"]: node["status"] for node in payload["nodes"]}
            self.assertEqual(status_by_id["workua:normalize"], "complete")
            self.assertEqual(status_by_id["workua:score"], "complete")
            self.assertEqual(status_by_id["workua:draft"], "complete")
            self.assertEqual(status_by_id["workua:review_approval"], "blocked_waiting_owner")
            self.assertEqual(status_by_id["workua:final_gated_apply_manual_handoff"], "blocked_waiting_owner")

            outreach_nodes = [node for node in payload["nodes"] if node["kind"] == "outreach_draft"]
            self.assertEqual(len(outreach_nodes), 1)
            self.assertFalse(outreach_nodes[0]["data"]["submission_allowed"])
            self.assertFalse(outreach_nodes[0]["data"]["upload_allowed"])

    def test_pipeline_gate_data_marks_owner_approval_gates(self) -> None:
        gates = {row["id"]: row for row in pipeline_gate_data()}
        self.assertFalse(gates["discover"]["requires_owner_approval"])
        self.assertTrue(gates["review_approval"]["requires_owner_approval"])
        self.assertTrue(gates["final_gated_apply_manual_handoff"]["requires_owner_approval"])

    def test_run_once_persists_review_artifacts_without_submit_flags(self) -> None:
        fetched_urls: list[str] = []

        def fake_fetch(url: str) -> str:
            fetched_urls.append(url)
            return PUBLIC_WORKUA_HTML

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary = run_workua_public_search_once(
                DiscoveryQuery(text="Python AI", limit=1),
                db_path=tmp_path / "workua.sqlite3",
                artifact_dir=tmp_path / "artifacts",
                fetcher=fake_fetch,
            )

            self.assertEqual(fetched_urls, ["https://www.work.ua/jobs-Python+AI/"])
            self.assertEqual(summary["schema"], "job.workua_run_once_summary.v0")
            self.assertEqual(len(summary["jobs_persisted"]), 1)
            self.assertEqual(len(summary["approval_artifacts"]), 1)
            self.assertEqual(summary["blocker_artifacts"], [])
            self.assertFalse(summary["submission_allowed"])
            self.assertFalse(summary["upload_allowed"])

            approval = summary["approval_artifacts"][0]
            self.assertTrue(Path(approval["artifact_path"]).exists())
            self.assertTrue(approval["approval_required"])
            self.assertFalse(approval["submission_allowed"])
            self.assertFalse(approval["upload_allowed"])
            self.assertIn("exact message", approval["required_owner_approval"].lower())

            status_by_id = {node["id"]: node["status"] for node in summary["progress_snapshot"]["nodes"]}
            self.assertEqual(status_by_id["workua:review_approval"], "blocked_waiting_owner")

    def test_run_once_writes_blocker_artifact_when_no_vacancies_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary = run_workua_public_search_once(
                DiscoveryQuery(text="No Match", limit=3),
                db_path=tmp_path / "workua.sqlite3",
                artifact_dir=tmp_path / "artifacts",
                fetcher=lambda _url: "<html><body>No jobs here</body></html>",
            )

            self.assertEqual(summary["jobs_persisted"], [])
            self.assertEqual(summary["approval_artifacts"], [])
            self.assertEqual(len(summary["blocker_artifacts"]), 1)
            blocker = summary["blocker_artifacts"][0]
            self.assertEqual(blocker["reason"], "no_public_vacancies_extracted")
            self.assertTrue(Path(blocker["artifact_path"]).exists())
            self.assertFalse(blocker["submission_allowed"])
            self.assertFalse(blocker["upload_allowed"])


if __name__ == "__main__":
    unittest.main()
