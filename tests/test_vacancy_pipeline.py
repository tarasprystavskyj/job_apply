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

    def test_inbox_offer_is_not_actionable_after_prior_reply_to_same_thread(self) -> None:
        row = {
            "source_url": "https://djinni.co/my/inbox/25880672/#last",
            "title": "Senior Python AI Engineer",
            "snippet": "Recruiter message",
            "recommendation": "review",
        }

        with patch.object(vacancy_pipeline, "previously_replied_thread_urls", return_value={"https://djinni.co/my/inbox/25880672/"}):
            actionable = vacancy_pipeline.is_actionable_inbox_offer(row)

        self.assertFalse(actionable)

    def test_inbox_navigation_rows_are_not_actionable(self) -> None:
        for title, url in [
            ("Відкрити", "https://djinni.co/jobs/832864-staff-lead-full-stack-engineer-ai-commerce/?ref=inbox_suggested"),
            ("Більше", "https://djinni.co/my/inbox/#"),
        ]:
            with self.subTest(title=title):
                actionable = vacancy_pipeline.is_actionable_inbox_offer(
                    {
                        "source_url": url,
                        "title": title,
                        "snippet": "",
                        "recommendation": "review",
                    }
                )

                self.assertFalse(actionable)

    def test_submission_history_reads_terminal_results_for_all_sites(self) -> None:
        events = [
            {
                "site": "workua",
                "source_url": "https://www.work.ua/jobs/8170878/",
                "result": "submitted_success",
                "attempted_at": "2026-06-12T10:00:00+0300",
            },
            {
                "site": "robotaua",
                "source_url": "https://robota.ua/company3685368/vacancy11052703",
                "result": "already_applied",
                "attempted_at": "2026-06-12T11:00:00+0300",
            },
            {
                "site": "dou",
                "source_url": "https://jobs.dou.ua/companies/example/vacancies/12345/",
                "result": "submit_clicked_unconfirmed",
                "after": {"url": "https://jobs.dou.ua/companies/example/vacancies/12345/?applied=ok"},
                "attempted_at": "2026-06-12T12:00:00+0300",
            },
        ]

        history = vacancy_pipeline.latest_submission_by_url(events)

        self.assertEqual(
            vacancy_pipeline.terminal_submission_state({"source_url": "https://www.work.ua/jobs/8170878/"}, history),
            "submitted_success",
        )
        self.assertEqual(
            vacancy_pipeline.terminal_submission_state({"source_url": "https://robota.ua/company3685368/vacancy11052703/"}, history),
            "already_applied",
        )
        self.assertEqual(
            vacancy_pipeline.terminal_submission_state({"source_url": "https://jobs.dou.ua/companies/example/vacancies/12345/"}, history),
            "submitted_success_inferred",
        )

    def test_candidate_batch_excludes_previously_submitted_public_vacancies(self) -> None:
        observation = {
            "source_site": "workua",
            "source_url": "https://www.work.ua/jobs/8170878/",
            "title": "Python Engineer",
            "company": "Netpeak",
            "fit_tags": ["python"],
        }
        submitted = {
            "source_url": "https://www.work.ua/jobs/8170878/",
            "result": "submitted_success",
            "attempted_at": "2026-06-12T10:00:00+0300",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "batch.csv"
            with (
                patch.object(vacancy_pipeline, "latest_observations", return_value=[observation]),
                patch.object(vacancy_pipeline, "latest_inbox_offers", return_value=[]),
                patch.object(vacancy_pipeline, "latest_submission_by_url", return_value=vacancy_pipeline.latest_submission_by_url([submitted])),
            ):
                vacancy_pipeline.build_candidate_batch(limit=10, output=output)

            text = output.read_text(encoding="utf-8")

        self.assertNotIn("https://www.work.ua/jobs/8170878/", text)

    def test_candidate_batch_excludes_previously_submitted_inbox_job_url(self) -> None:
        inbox_offer = {
            "source_site": "djinni_inbox",
            "source_url": "https://djinni.co/jobs/832864-staff-lead-full-stack-engineer-ai-commerce/?ref=inbox_suggested",
            "title": "Staff / Lead Full-Stack Engineer (AI / Commerce)",
            "company": "Example",
            "snippet": "Relevant suggested job",
            "recommendation": "review",
            "score": 50,
        }
        submitted = {
            "source_url": "https://djinni.co/jobs/832864-staff-lead-full-stack-engineer-ai-commerce/",
            "result": "submitted_success",
            "attempted_at": "2026-06-19T19:00:00+0300",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "batch.csv"
            with (
                patch.object(vacancy_pipeline, "latest_observations", return_value=[]),
                patch.object(vacancy_pipeline, "latest_inbox_offers", return_value=[inbox_offer]),
                patch.object(vacancy_pipeline, "latest_submission_by_url", return_value=vacancy_pipeline.latest_submission_by_url([submitted])),
            ):
                vacancy_pipeline.build_candidate_batch(limit=10, output=output)

            text = output.read_text(encoding="utf-8")

        self.assertNotIn("832864-staff-lead-full-stack-engineer-ai-commerce", text)

    def test_terminal_submission_history_survives_later_blocker_noise(self) -> None:
        events = [
            {
                "source_url": "https://www.work.ua/jobs/8170878/",
                "result": "submitted_success",
                "attempted_at": "2026-06-12T10:00:00+0300",
            },
            {
                "source_url": "https://www.work.ua/jobs/8170878/",
                "result": "blocked_no_application_surface",
                "attempted_at": "2026-06-12T11:00:00+0300",
            },
        ]

        history = vacancy_pipeline.latest_submission_by_url(events)

        self.assertEqual(
            vacancy_pipeline.terminal_submission_state({"source_url": "https://www.work.ua/jobs/8170878/"}, history),
            "submitted_success",
        )

    def test_submission_history_matches_urls_with_query_fragment_and_trailing_slash(self) -> None:
        events = [
            {
                "source_url": "https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/?applied=ok#done",
                "result": "submitted_success",
                "attempted_at": "2026-06-12T10:00:00+0300",
            }
        ]

        history = vacancy_pipeline.latest_submission_by_url(events)

        self.assertEqual(
            vacancy_pipeline.terminal_submission_state(
                {"source_url": "https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/"},
                history,
            ),
            "submitted_success",
        )

    def test_candidate_batch_excludes_djinni_eligibility_mismatch_warning(self) -> None:
        observation = {
            "source_site": "djinni",
            "source_url": "https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/",
            "title": "Senior Python Backend Engineer",
            "company": "Visarsoft",
            "fit_tags": ["python", "ai"],
        }
        mismatch = {
            "source_url": "https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/",
            "result": "blocked_no_apply_form",
            "blocked_reason": "djinni eligibility mismatch: salary/location filters",
            "eligibility_mismatch": True,
            "attempted_at": "2026-06-19T15:34:37+0300",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "batch.csv"
            with (
                patch.object(vacancy_pipeline, "latest_observations", return_value=[observation]),
                patch.object(vacancy_pipeline, "latest_inbox_offers", return_value=[]),
                patch.object(vacancy_pipeline, "latest_submission_by_url", return_value=vacancy_pipeline.latest_submission_by_url([mismatch])),
            ):
                vacancy_pipeline.build_candidate_batch(limit=10, output=output)

            text = output.read_text(encoding="utf-8")

        self.assertNotIn("https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/", text)

    def test_candidate_batch_excludes_active_blocked_retry_rows(self) -> None:
        observation = {
            "source_site": "dou",
            "source_url": "https://jobs.dou.ua/companies/spsoft/vacancies/361793/",
            "title": "Python Engineer",
            "company": "SPsoft",
            "fit_tags": ["python"],
        }
        blocked = {
            "source_url": "https://jobs.dou.ua/companies/spsoft/vacancies/361793/",
            "result": "blocked_no_application_surface",
            "attempted_at": "2026-06-12T20:12:15+0300",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "batch.csv"
            with (
                patch.object(vacancy_pipeline, "latest_observations", return_value=[observation]),
                patch.object(vacancy_pipeline, "latest_inbox_offers", return_value=[]),
                patch.object(vacancy_pipeline, "latest_submission_by_url", return_value=vacancy_pipeline.latest_submission_by_url([blocked])),
            ):
                vacancy_pipeline.build_candidate_batch(limit=10, output=output)

            text = output.read_text(encoding="utf-8")

        self.assertNotIn("https://jobs.dou.ua/companies/spsoft/vacancies/361793/", text)

    def test_candidate_batch_excludes_manual_skipped_urls(self) -> None:
        observation = {
            "source_site": "dou",
            "source_url": "https://jobs.dou.ua/companies/epam-systems/vacancies/358381/",
            "title": "Python Team Lead",
            "company": "EPAM",
            "fit_tags": ["python"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            skip_path = Path(tmp) / "manual_skips.jsonl"
            skip_path.write_text(
                json.dumps(
                    {
                        "status": "manual_skip",
                        "source_url": "https://jobs.dou.ua/companies/epam-systems/vacancies/358381/",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = Path(tmp) / "batch.csv"
            with (
                patch.object(vacancy_pipeline, "latest_observations", return_value=[observation]),
                patch.object(vacancy_pipeline, "latest_inbox_offers", return_value=[]),
                patch.object(vacancy_pipeline, "latest_submission_by_url", return_value={}),
                patch.object(vacancy_pipeline, "MANUAL_SKIPS_PATH", skip_path),
            ):
                vacancy_pipeline.build_candidate_batch(limit=10, output=output)

            text = output.read_text(encoding="utf-8")

        self.assertNotIn("https://jobs.dou.ua/companies/epam-systems/vacancies/358381/", text)


if __name__ == "__main__":
    unittest.main()
