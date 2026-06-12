from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_platforms import ApprovalGate, DiscoveryQuery, default_registry  # noqa: E402
from job_platforms.base import UnsupportedAction  # noqa: E402
from job_platforms.dou import (  # noqa: E402
    DouAdapter,
    build_dou_progress_snapshot,
    build_dou_review_drafts,
    exact_dou_vacancy_observations,
    is_dou_listing_url,
    score_dou_observation,
)
from job_platforms.platforms import DouAdapter as PlatformsDouAdapter  # noqa: E402
from dou_pipeline import run_once as run_dou_once  # noqa: E402
from shared_job_db import fetch_progress_rows  # noqa: E402


PUBLIC_DOU_HTML = """
<!doctype html>
<html>
<head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Senior Python AI Automation Engineer",
    "url": "https://jobs.dou.ua/companies/example-tech/vacancies/123456/",
    "hiringOrganization": {"@type": "Organization", "name": "Example Tech"},
    "jobLocation": {"@type": "Place", "address": {"addressLocality": "Lviv", "addressCountry": "UA"}},
    "description": "Build Python services, LLM workflows, FastAPI integrations, and automation.",
    "datePosted": "2026-06-10",
    "employmentType": "FULL_TIME"
  }
  </script>
</head>
<body>
  <ul>
    <li class="l-vacancy">
      <div class="date">11 червня</div>
      <div class="title">
        <a class="vt" href="/companies/relocate-example/vacancies/789/">Python Backend Engineer</a>
        <strong class="salary">$3000-5000</strong>
        в <a class="company" href="/companies/relocate-example/">Relocate Example</a>
        <span class="cities">за кордоном, віддалено</span>
      </div>
      <div class="sh-info">Backend API automation with Python, Docker, and LLM tooling.</div>
    </li>
  </ul>
</body>
</html>
"""

DOU_COMPANY_VACANCIES_LINK_HTML = """
<!doctype html>
<html>
<body>
  <ul>
    <li class="l-vacancy">
      <div class="date">12 червня</div>
      <div class="title">
        <a class="vt" href="https://jobs.dou.ua/companies/transhimtreid/vacancies/361890/">Senior Python Backend, AI Engineer</a>
        <strong>в&nbsp;<a class="company" href="https://jobs.dou.ua/companies/transhimtreid/vacancies/">ТОВ "Трансхімтрейд"</a></strong>
        <span class="cities">віддалено</span>
      </div>
      <div class="sh-info">Python backend and AI production engineering.</div>
    </li>
  </ul>
</body>
</html>
"""


class DouAdapterTest(unittest.TestCase):
    def test_discovery_urls_cover_jobs_and_relocate(self) -> None:
        adapter = DouAdapter()
        urls = adapter.discovery_urls(DiscoveryQuery(text="Python AI"))
        self.assertEqual(
            urls,
            [
                "https://jobs.dou.ua/vacancies/?search=Python+AI",
                "https://relocate.dou.ua/jobs/?search=Python+AI",
            ],
        )

    def test_extract_public_json_ld_and_listing_vacancies(self) -> None:
        adapter = DouAdapter()
        observations = adapter.extract_public_vacancies(
            PUBLIC_DOU_HTML,
            "https://relocate.dou.ua/jobs/?search=Python+AI",
            DiscoveryQuery(text="Python AI"),
        )
        self.assertEqual(len(observations), 2)
        first = observations[0]
        self.assertEqual(first.source_site, "dou")
        self.assertEqual(first.company, "Example Tech")
        self.assertIn("python", first.fit_tags)
        self.assertIn("llm", first.fit_tags)
        self.assertIn("manual_handoff_only", first.risk_flags)

        second = observations[1]
        self.assertEqual(second.company, "Relocate Example")
        self.assertTrue(second.source_url.startswith("https://relocate.dou.ua/companies/relocate-example/vacancies/789/"))
        self.assertEqual(second.salary_hint, "$3000-5000")
        self.assertGreater(score_dou_observation(second), 0)

    def test_listing_remediation_keeps_only_exact_vacancy_urls(self) -> None:
        adapter = DouAdapter()
        observations = adapter.extract_public_vacancies(
            PUBLIC_DOU_HTML,
            "https://relocate.dou.ua/jobs/?category=Python&from=maybe",
            DiscoveryQuery(text="Python AI"),
        )
        exact = exact_dou_vacancy_observations(observations)

        self.assertTrue(is_dou_listing_url("https://relocate.dou.ua/jobs/?category=Python&from=maybe"))
        self.assertEqual(len(exact), 2)
        self.assertTrue(all("/vacancies/" in observation.source_url for observation in exact))

    def test_company_vacancies_link_does_not_replace_exact_vacancy_url(self) -> None:
        adapter = DouAdapter()
        observations = adapter.extract_public_vacancies(
            DOU_COMPANY_VACANCIES_LINK_HTML,
            "https://jobs.dou.ua/vacancies/?search=Python+AI",
            DiscoveryQuery(text="Python AI"),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(
            observations[0].source_url,
            "https://jobs.dou.ua/companies/transhimtreid/vacancies/361890/",
        )
        self.assertEqual(observations[0].company, 'ТОВ "Трансхімтрейд"')

    def test_prepare_and_submit_are_not_implemented(self) -> None:
        adapter = DouAdapter()
        vacancy = adapter.normalize_vacancy(
            {
                "source_url": "https://jobs.dou.ua/companies/example/vacancies/1/",
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

    def test_registry_and_platforms_reexport_use_new_adapter(self) -> None:
        adapter = default_registry().get("dou")
        self.assertIsInstance(adapter, DouAdapter)
        self.assertIs(PlatformsDouAdapter, DouAdapter)

    def test_shared_db_review_drafts_and_progress_are_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.sqlite3"
            adapter = DouAdapter()
            observations = adapter.extract_public_vacancies(
                PUBLIC_DOU_HTML,
                "https://jobs.dou.ua/vacancies/?search=Python+AI",
                DiscoveryQuery(text="Python AI"),
            )

            result = build_dou_review_drafts(observations, db_path, limit=5)
            self.assertEqual(result["jobs"], 2)
            self.assertEqual(result["outreach"], 2)

            rows = fetch_progress_rows(db_path)
            self.assertEqual(len([row for row in rows["jobs"] if row["platform"] == "dou"]), 2)
            for draft in rows["outreach"]:
                self.assertEqual(draft["approval_status"], "needs_owner_review")
                self.assertFalse(bool(draft["submission_allowed"]))
                self.assertFalse(bool(draft["upload_allowed"]))

            snapshot = build_dou_progress_snapshot(db_path)
            self.assertEqual(snapshot.schema, "job.progress_snapshot.langgraph.v0")
            status_by_id = {node.id: node.status for node in snapshot.nodes}
            self.assertEqual(status_by_id["normalize"], "complete")
            self.assertEqual(status_by_id["score"], "complete")
            self.assertEqual(status_by_id["draft"], "complete")
            self.assertEqual(status_by_id["review_approval"], "blocked_waiting_owner")
            self.assertEqual(status_by_id["final_gated_apply"], "blocked_waiting_owner")

    def test_run_once_fetches_public_pages_and_writes_review_artifacts(self) -> None:
        fetched: list[str] = []

        def fake_fetch(url: str, timeout: int) -> str:
            fetched.append(url)
            return PUBLIC_DOU_HTML

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            summary = run_dou_once(
                "Python AI",
                limit=1,
                max_pages=1,
                db_path=base / "jobs.sqlite3",
                observations_path=base / "observations.jsonl",
                progress_path=base / "progress.json",
                summary_path=base / "summary.json",
                blockers_path=base / "blockers.json",
                fetch_text=fake_fetch,
            )

            self.assertEqual(fetched, ["https://jobs.dou.ua/vacancies/?search=Python+AI"])
            self.assertEqual(summary["observations"], 1)
            self.assertFalse(summary["safety"]["submission_allowed"])
            self.assertFalse(summary["safety"]["upload_allowed"])

            observations = (base / "observations.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(observations), 1)
            progress = (base / "progress.json").read_text(encoding="utf-8")
            self.assertIn("final_gated_apply", progress)
            blockers = (base / "blockers.json").read_text(encoding="utf-8")
            self.assertIn("submit/apply/send", blockers)

            rows = fetch_progress_rows(base / "jobs.sqlite3")
            self.assertEqual(len(rows["jobs"]), 1)
            self.assertEqual(len(rows["outreach"]), 1)
            self.assertFalse(bool(rows["outreach"][0]["submission_allowed"]))

    def test_run_once_remediates_listing_url_into_exact_review_artifacts(self) -> None:
        fetched: list[str] = []

        def fake_fetch(url: str, timeout: int) -> str:
            fetched.append(url)
            return PUBLIC_DOU_HTML

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            summary = run_dou_once(
                "Python AI",
                urls=["https://relocate.dou.ua/jobs/?category=Python&from=maybe"],
                limit=5,
                max_pages=1,
                db_path=base / "jobs.sqlite3",
                observations_path=base / "observations.jsonl",
                progress_path=base / "progress.json",
                summary_path=base / "summary.json",
                blockers_path=base / "blockers.json",
                remediations_path=base / "remediations.jsonl",
                fetch_text=fake_fetch,
            )

            self.assertEqual(fetched, ["https://relocate.dou.ua/jobs/?category=Python&from=maybe"])
            self.assertEqual(summary["listing_remediations"]["attempted"], 1)
            self.assertEqual(summary["listing_remediations"]["blocked"], 0)
            self.assertEqual(summary["observations"], 2)

            remediations = [json.loads(line) for line in (base / "remediations.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(remediations[0]["status"], "exact_urls_extracted")
            self.assertEqual(len(remediations[0]["exact_urls"]), 2)

            observations = [json.loads(line) for line in (base / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all("/vacancies/" in row["source_url"] for row in observations))

    def test_run_once_refuses_non_dou_urls_before_fetch(self) -> None:
        def fake_fetch(url: str, timeout: int) -> str:
            raise AssertionError("non-DOU URL should not be fetched")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                run_dou_once(
                    "Python AI",
                    urls=["https://example.com/jobs"],
                    db_path=Path(tmp) / "jobs.sqlite3",
                    observations_path=Path(tmp) / "observations.jsonl",
                    progress_path=Path(tmp) / "progress.json",
                    summary_path=Path(tmp) / "summary.json",
                    blockers_path=Path(tmp) / "blockers.json",
                    fetch_text=fake_fetch,
                )


if __name__ == "__main__":
    unittest.main()
