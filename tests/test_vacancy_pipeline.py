from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import vacancy_pipeline  # noqa: E402
from job_platforms.models import VacancyObservation  # noqa: E402
from shared_job_db import upsert_job  # noqa: E402


class VacancyPipelineTests(unittest.TestCase):
    def test_latest_observations_filters_dou_rows_that_are_not_exact_vacancy_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observations = Path(tmp) / "observations.jsonl"
            rows = [
                {
                    "source_site": "dou",
                    "source_url": "https://relocate.dou.ua/jobs/?category=Python&from=maybe",
                    "title": "Listing URL",
                },
                {
                    "source_site": "dou",
                    "source_url": "https://jobs.dou.ua/companies/example/vacancies/",
                    "title": "Company vacancies URL",
                },
                {
                    "source_site": "dou",
                    "source_url": "https://jobs.dou.ua/companies/example/vacancies/12345/",
                    "title": "Exact vacancy URL",
                },
                {
                    "source_site": "workua",
                    "source_url": "https://www.work.ua/jobs/8170878/",
                    "title": "Work.ua row",
                },
            ]
            observations.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
                encoding="utf-8",
            )

            with (
                patch.object(vacancy_pipeline, "OBSERVATION_PATHS", [observations]),
                patch.object(vacancy_pipeline, "DEFAULT_DB_PATH", Path(tmp) / "empty.sqlite3"),
            ):
                result = vacancy_pipeline.latest_observations()

            self.assertEqual([row["title"] for row in result], ["Exact vacancy URL", "Work.ua row"])

    def test_latest_observations_includes_shared_db_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.sqlite3"
            upsert_job(
                VacancyObservation(
                    source_site="workua",
                    source_url="https://www.work.ua/jobs/8170878/",
                    title="Python Engineer",
                    company="Netpeak",
                    status="observed",
                    fit_tags=("python", "ai"),
                ),
                score=42,
                db_path=db_path,
            )
            upsert_job(
                VacancyObservation(
                    source_site="dou",
                    source_url="https://jobs.dou.ua/companies/example/vacancies/",
                    title="Invalid DOU listing",
                    company="Example",
                    status="observed",
                    fit_tags=("python",),
                ),
                score=99,
                db_path=db_path,
            )
            upsert_job(
                VacancyObservation(
                    source_site="dou",
                    source_url="https://jobs.dou.ua/companies/example/vacancies/12345/",
                    title="Exact DOU",
                    company="Example",
                    status="observed",
                    fit_tags=("python",),
                ),
                score=77,
                db_path=db_path,
            )

            with (
                patch.object(vacancy_pipeline, "OBSERVATION_PATHS", []),
                patch.object(vacancy_pipeline, "DEFAULT_DB_PATH", db_path),
            ):
                result = vacancy_pipeline.latest_observations()

            titles = {row["title"] for row in result}
            self.assertEqual(titles, {"Python Engineer", "Exact DOU"})
            self.assertNotIn("Invalid DOU listing", titles)

    def test_cover_letter_sanitizes_contaminated_title_and_avoids_agentic_phrasing(self) -> None:
        vacancy = {
            "source_site": "robotaua",
            "source_url": "https://robota.ua/company127046/vacancy11178455",
            "title": (
                "Python AI Engineer Universal Bank/Універсал Банк Київ Universal Bank є сучасним "
                "українським банком зі стабільною репутацією протягом 30 років."
            ),
            "company": "",
            "fit_tags": ["python", "ai"],
        }
        resume = {"name": "Stanislav_Shcherbak_ai_dev_eng (1).pdf"}

        message = vacancy_pipeline.draft_cover_letter(vacancy, resume)

        self.assertIn('the "Python AI Engineer" role', message)
        self.assertNotIn("Universal Bank є сучасним", message)
        self.assertNotIn("Hi team team", message)
        self.assertNotIn("I would position", message)
        self.assertNotIn("I should be transparent", message)
        self.assertNotIn("Stanislav_Shcherbak", message)
        self.assertNotIn(".pdf", message)
        self.assertNotIn("review artifacts", message)
        self.assertNotIn("diffs", message)
        self.assertNotIn("scoped tool use", message)

    def test_quality_gate_rejects_resume_filename_and_internal_phrasing(self) -> None:
        errors = vacancy_pipeline.draft_quality_errors(
            "Hi team team,\n\nI would position the most relevant resume as Stanislav_Shcherbak_ai_dev_eng (1).pdf."
            " I use review artifacts, diffs, and scoped tool use."
        )

        self.assertIn("banned phrase: team team", errors)
        self.assertIn("banned phrase: I would position", errors)
        self.assertIn("banned phrase: .pdf", errors)
        self.assertIn("banned phrase: review artifacts", errors)


if __name__ == "__main__":
    unittest.main()
