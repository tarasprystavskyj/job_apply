from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import job_apply_web  # noqa: E402
from job_apply_web import approved_review_rows, approved_rows_by_site, submit_blocker, supported_submit_site, update_batch_approvals  # noqa: E402
from job_apply_web import supported_label  # noqa: E402


MESSAGE = (
    "Hi team, I am interested in this role because it matches my Python, AI automation, "
    "backend delivery, and pragmatic product engineering background."
)
LINKEDIN = "https://www.linkedin.com/in/taras-prystavskyj/"


def write_batch(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "site",
        "url",
        "title",
        "company",
        "message",
        "message_file",
        "salary_usd",
        "linkedin",
        "resume_policy",
        "approved_resume_name",
        "approved_to_submit",
        "final_submit_allowed",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def row(site: str, url: str) -> dict[str, str]:
    return {
        "site": site,
        "url": url,
        "title": "Python Engineer",
        "company": "Example",
        "message": MESSAGE,
        "message_file": "",
        "salary_usd": "3000",
        "linkedin": LINKEDIN,
        "resume_policy": "no_resume",
        "approved_resume_name": "",
        "approved_to_submit": "false",
        "final_submit_allowed": "false",
    }


class JobApplyWebTests(unittest.TestCase):
    def test_supported_submit_site_accepts_four_exact_public_job_url_shapes(self) -> None:
        cases = [
            (row("djinni", "https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/"), "djinni"),
            (row("workua", "https://www.work.ua/jobs/8170878/"), "workua"),
            (row("dou", "https://jobs.dou.ua/vacancies/123456/"), "dou"),
            (row("robotaua", "https://robota.ua/company3685368/vacancy11052703"), "robotaua"),
        ]

        for input_row, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(supported_submit_site(input_row), expected)

    def test_dou_search_urls_are_visible_but_blocked_for_final_apply(self) -> None:
        input_row = row("dou", "https://relocate.dou.ua/jobs/?category=Python&from=maybe")

        self.assertEqual(supported_submit_site(input_row), "")
        self.assertIn("exact vacancy URL", submit_blocker(input_row))

    def test_djinni_inbox_rows_are_labeled_as_review_flow_not_supported_no(self) -> None:
        input_row = row("djinni_inbox", "https://djinni.co/my/inbox/25880123/#last")

        self.assertEqual(supported_submit_site(input_row), "")
        self.assertEqual(supported_label(input_row), "Djinni inbox")
        self.assertIn("&#8635;", job_apply_web.supported_cell_html(input_row))
        self.assertIn("review/reply flow", submit_blocker(input_row))

    def test_supported_cell_uses_checkmark_instead_of_site_name(self) -> None:
        input_row = row("workua", "https://www.work.ua/jobs/8170878/")

        cell = job_apply_web.supported_cell_html(input_row)

        self.assertIn("&#10003;", cell)
        self.assertIn("Work.ua final submit adapter is supported", cell)
        self.assertNotIn(">Work.ua<", cell)

    def test_web_run_log_tail_infers_submitted_success_from_sent_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            log_path = log_dir / "workua_resume_only_submit_20260612.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "site": "workua",
                        "attempted_at": "2026-06-12T14:41:38+0300",
                        "company": "Netpeak",
                        "result": "submit_clicked_unconfirmed",
                        "source_url": "https://www.work.ua/jobs/8170878/",
                        "submitted": {"submitted": True},
                        "after": {"url": "https://www.work.ua/jobseeker/my/resumes/sent/?job_id=8170878"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with (
                patch.object(job_apply_web, "WEB_RUNS_DIR", log_dir),
                patch.object(job_apply_web, "SUBMISSION_LOGS", {}),
            ):
                events = job_apply_web.submission_log_events(limit=3)

            self.assertEqual(len(events), 1)
            self.assertEqual(job_apply_web.display_result(events[0]), "submitted_success_inferred")

    def test_render_page_supports_ukrainian_localization(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            state.write_text(json.dumps({"latest_batch": "", "status": "idle"}), encoding="utf-8")
            with patch.object(job_apply_web, "STATE_PATH", state):
                html = job_apply_web.render_page("uk")

            self.assertIn("Автоматизація подачі заявок", html)
            self.assertIn("Надіслані заявки", html)
            self.assertIn("?lang=en", html)

    def test_render_page_exposes_all_sources_scan_and_function_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            state.write_text(json.dumps({"latest_batch": "", "status": "idle"}), encoding="utf-8")
            with patch.object(job_apply_web, "STATE_PATH", state):
                html = job_apply_web.render_page("en")

            self.assertIn("Scan all sources + build batch", html)
            self.assertIn("/function-map", html)
            self.assertIn("Function map", html)

    def test_load_state_uses_newer_all_sources_summary_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            old_batch = base / "candidate_batch_old.csv"
            new_batch = base / "candidate_batch_new.csv"
            old_batch.write_text("site,url\n", encoding="utf-8")
            new_batch.write_text("site,url\ndou,https://jobs.dou.ua/companies/example/vacancies/12345/\n", encoding="utf-8")
            state = base / "state.json"
            summary = base / "all_sources_scan_summary.json"
            state.write_text(json.dumps({"latest_batch": str(old_batch), "status": "old"}), encoding="utf-8")
            summary.write_text(
                json.dumps(
                    {
                        "batch": str(new_batch),
                        "notes": {"dou": {"observations": 1}},
                        "errors": {},
                    }
                ),
                encoding="utf-8",
            )
            old_time = time.time() - 20
            new_time = time.time()
            os.utime(old_batch, (old_time, old_time))
            os.utime(new_batch, (new_time, new_time))

            with (
                patch.object(job_apply_web, "STATE_PATH", state),
                patch.object(job_apply_web, "ALL_SOURCES_SUMMARY_PATH", summary),
            ):
                loaded = job_apply_web.load_state()

            self.assertEqual(loaded["latest_batch"], str(new_batch))
            self.assertIn("dou=ok", loaded["status"])

    def test_function_map_page_is_interactive_and_has_json_endpoint_data(self) -> None:
        html = job_apply_web.render_function_map_page("en")
        graph = job_apply_web.function_graph()

        self.assertIn("groupFilter", html)
        self.assertIn("Save graph note", html)
        self.assertIn("Message QA gate", html)
        self.assertIn("data-detail=", html)
        self.assertTrue(any(node["id"] == "source_scan" for node in graph["nodes"]))

    def test_function_map_notes_persist_comments_and_subtasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            notes_path = Path(td) / "function_graph_notes.json"

            with patch.object(job_apply_web, "FUNCTION_GRAPH_NOTES_PATH", notes_path):
                payload = job_apply_web.save_function_graph_note(
                    "graph_tz",
                    status_note="MVP accepts local notes before full graph editing.",
                    comment="Owner asked for graph comment support.",
                    subtask="Add resolved-subtask controls later.",
                )
                html = job_apply_web.render_function_map_page("en")
                graph = job_apply_web.function_graph_with_notes()

            note = payload["nodes"]["graph_tz"]
            self.assertEqual(note["status_note"], "MVP accepts local notes before full graph editing.")
            self.assertEqual(note["comments"][0]["text"], "Owner asked for graph comment support.")
            self.assertEqual(note["subtasks"][0]["status"], "open")
            self.assertIn("Requirements Notes", html)
            self.assertIn("Owner asked for graph comment support.", html)
            self.assertIn("function_graph_notes.v0", graph["notes"]["schema"])

    def test_sent_applications_page_lists_successful_events_with_open_link(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            log_path = log_dir / "robotaua_submit_20260612.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "site": "robotaua",
                        "attempted_at": "2026-06-12T15:25:33+0300",
                        "company": "UEB",
                        "title": "AI Software Engineer",
                        "result": "submitted_success",
                        "source_url": "https://robota.ua/company3685368/vacancy11052703",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(job_apply_web, "WEB_RUNS_DIR", log_dir),
                patch.object(job_apply_web, "SUBMISSION_LOGS", {}),
            ):
                html = job_apply_web.render_sent_applications_page("uk")

            self.assertIn("Надіслані заявки", html)
            self.assertIn("UEB", html)
            self.assertIn(">open</a>", html)
            self.assertIn("submitted_success", html)

    def test_sent_applications_scans_full_submission_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            canonical = Path(td) / "djinni_submission.jsonl"
            rows = [
                {
                    "site": "djinni",
                    "attempted_at": "2026-06-11T13:05:00+0300",
                    "company": "Sigma Software",
                    "title": "Senior Python AI Engineer",
                    "result": "submitted_success",
                    "source_url": "https://djinni.co/jobs/825969-senior-python-ai-engineer/",
                }
            ]
            rows.extend(
                {
                    "site": "djinni",
                    "attempted_at": f"2026-06-11T15:{idx:02d}:00+0300",
                    "company": "Placeholder",
                    "title": "Placeholder",
                    "result": "dry_run_ok",
                    "source_url": f"https://djinni.co/jobs/placeholder-{idx}/",
                }
                for idx in range(12)
            )
            canonical.write_text("\n".join(json.dumps(item) for item in rows) + "\n", encoding="utf-8")
            with (
                patch.object(job_apply_web, "WEB_RUNS_DIR", Path(td) / "empty"),
                patch.object(job_apply_web, "SUBMISSION_LOGS", {"djinni": canonical}),
            ):
                html = job_apply_web.render_sent_applications_page("en")

            self.assertIn("Sigma Software", html)
            self.assertIn("submitted_success", html)

    def test_djinni_inbox_review_rows_can_be_approved_and_dry_run_logged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            write_batch(batch, [row("djinni_inbox", "https://djinni.co/my/inbox/25880123/#last")])

            changed, skipped = update_batch_approvals(batch, approve_all=True)
            with patch.object(job_apply_web, "WEB_RUNS_DIR", Path(td)):
                jobs = job_apply_web.launch_approved_runs(batch, "dry-run")

            self.assertEqual((changed, skipped), (1, 0))
            self.assertEqual(jobs[0]["site"], "djinni_inbox")
            self.assertEqual(jobs[0]["rows"], 1)
            log_rows = [
                json.loads(line)
                for line in Path(str(jobs[0]["jsonl_log"])).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(log_rows[0]["result"], "dry_run_ok")
            self.assertIn("message_digest", log_rows[0])

    def test_duplicate_djinni_inbox_review_rows_are_deduped_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            first = row("djinni_inbox", "https://djinni.co/my/inbox/25880672/#last")
            second = row("djinni_inbox", "https://djinni.co/my/inbox/25880672/")
            for item in [first, second]:
                item["approved_to_submit"] = "true"
                item["final_submit_allowed"] = "true"
            write_batch(batch, [first, second])

            rows = approved_review_rows(batch)

        self.assertEqual(len(rows), 1)

    def test_approve_all_marks_only_supported_rows_and_adds_live_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            write_batch(
                batch,
                [
                    row("djinni", "https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/"),
                    row("workua", "https://www.work.ua/jobs/8170878/"),
                    row("dou", "https://relocate.dou.ua/jobs/?category=Python"),
                ],
            )

            changed, skipped = update_batch_approvals(batch, approve_all=True)
            grouped = approved_rows_by_site(batch)
            with batch.open("r", encoding="utf-8-sig", newline="") as f:
                fields = list(csv.DictReader(f).fieldnames or [])

            self.assertEqual(changed, 2)
            self.assertEqual(skipped, 1)
            self.assertEqual(len(grouped["djinni"]), 1)
            self.assertEqual(len(grouped["workua"]), 1)
            self.assertEqual(len(grouped["dou"]), 0)
            self.assertIn("linkedin_policy", fields)
            self.assertIn("upload_allowed", fields)

    def test_launch_routes_only_exact_djinni_jobs_to_djinni_submitter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            rows = [
                row("djinni", "https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/"),
                row("djinni_inbox", "https://djinni.co/my/inbox/25880123/#last"),
                row("dou", "https://relocate.dou.ua/jobs/?category=Python"),
            ]
            for item in rows:
                item["approved_to_submit"] = "true"
                item["final_submit_allowed"] = "true"
            write_batch(batch, rows)

            class FakeProcess:
                pid = 12345

            popen_commands: list[list[str]] = []

            def fake_popen(cmd: list[str], **kwargs: object) -> FakeProcess:
                popen_commands.append(cmd)
                return FakeProcess()

            with (
                patch.object(job_apply_web, "WEB_RUNS_DIR", Path(td)),
                patch.object(job_apply_web.subprocess, "Popen", side_effect=fake_popen),
            ):
                jobs = job_apply_web.launch_approved_runs(batch, "dry-run")

            grouped = approved_rows_by_site(batch)
            self.assertEqual([row["url"] for row in grouped["djinni"]], ["https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/"])
            self.assertEqual(grouped["dou"], [])
            self.assertEqual([job["site"] for job in jobs], ["djinni", "djinni_inbox"])
            self.assertEqual(len(popen_commands), 1)
            self.assertIn("djinni_csv_apply.py", popen_commands[0][1])
            csv_path = Path(str(jobs[0]["csv"]))
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                submitted_rows = list(csv.DictReader(f))
            self.assertEqual(len(submitted_rows), 1)
            self.assertEqual(submitted_rows[0]["site"], "djinni")
            self.assertTrue(submitted_rows[0]["url"].startswith("https://djinni.co/jobs/"))

    def test_approved_rows_are_normalized_for_site_specific_submitters(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            write_batch(
                batch,
                [
                    row("workua", "https://www.work.ua/jobs/8170878/"),
                    row("dou", "https://jobs.dou.ua/vacancies/123456/"),
                    row("robotaua", "https://robota.ua/company3685368/vacancy11052703"),
                ],
            )

            update_batch_approvals(batch, approve_all=True)
            grouped = approved_rows_by_site(batch)

            self.assertEqual(grouped["workua"][0]["linkedin_policy"], "fill_field")
            self.assertEqual(grouped["dou"][0]["linkedin_policy"], "fill_url")
            self.assertEqual(grouped["robotaua"][0]["linkedin_policy"], "include_linkedin")
            for rows in grouped.values():
                for approved in rows:
                    self.assertEqual(approved["resume_policy"], "no_resume")
                    self.assertEqual(approved["upload_allowed"], "false")
                    self.assertEqual(approved["approved_to_submit"], "true")
                    self.assertEqual(approved["final_submit_allowed"], "true")

    def test_robotaua_exact_upload_gate_is_preserved_when_already_approved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch.csv"
            values = row("robotaua", "https://robota.ua/company3685368/vacancy11052703")
            values["resume_policy"] = "upload_exact_resume"
            values["upload_allowed"] = "true"
            values["approved_resume_name"] = "Senior Python CV.pdf"
            values["approved_resume_path"] = "data/approved_artifacts/Senior Python CV.pdf"
            fields = list(values)
            with batch.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerow(values)

            changed, skipped = update_batch_approvals(batch, approve_all=True)
            grouped = approved_rows_by_site(batch)

            self.assertEqual((changed, skipped), (1, 0))
            approved = grouped["robotaua"][0]
            self.assertEqual(approved["resume_policy"], "upload_exact_resume")
            self.assertEqual(approved["upload_allowed"], "true")
            self.assertEqual(approved["approved_resume_name"], "Senior Python CV.pdf")
            self.assertEqual(approved["approved_resume_path"], "data/approved_artifacts/Senior Python CV.pdf")

    def test_dou_remediation_reads_raw_listing_rows_before_batch_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            observations = Path(td) / "observations.jsonl"
            observations.write_text(
                json.dumps(
                    {
                        "source_site": "dou",
                        "source_url": "https://relocate.dou.ua/jobs/?category=Python&from=maybe",
                        "title": "Listing row",
                    }
                ),
                encoding="utf-8",
            )
            calls: list[dict[str, object]] = []

            def fake_run_once(query_text: str, **kwargs: object) -> dict[str, object]:
                calls.append({"query_text": query_text, **kwargs})
                return {"listing_remediations": {"exact_urls_extracted": 2, "blocked": 0}}

            with (
                patch.object(job_apply_web, "OBSERVATION_PATHS", [observations]),
                patch.object(job_apply_web, "run_dou_once", side_effect=fake_run_once),
            ):
                note = job_apply_web.remediate_dou_listing_observations()

            self.assertIn("exact_urls=2", note)
            self.assertEqual(calls[0]["urls"], ["https://relocate.dou.ua/jobs/?category=Python&from=maybe"])

    def test_dou_remediation_falls_back_to_default_discovery_when_listing_rows_extract_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            observations = Path(td) / "observations.jsonl"
            observations.write_text(
                json.dumps(
                    {
                        "source_site": "dou",
                        "source_url": "https://relocate.dou.ua/jobs/?category=Python&from=maybe",
                        "title": "Listing row",
                    }
                ),
                encoding="utf-8",
            )
            calls: list[dict[str, object]] = []

            def fake_run_once(query_text: str, **kwargs: object) -> dict[str, object]:
                calls.append({"query_text": query_text, **kwargs})
                if kwargs.get("urls"):
                    return {"listing_remediations": {"exact_urls_extracted": 0, "blocked": 1}}
                return {"listing_remediations": {"exact_urls_extracted": 3, "blocked": 0}}

            with (
                patch.object(job_apply_web, "OBSERVATION_PATHS", [observations]),
                patch.object(job_apply_web, "run_dou_once", side_effect=fake_run_once),
            ):
                note = job_apply_web.remediate_dou_listing_observations()

            self.assertIn("exact_urls=3", note)
            self.assertEqual(calls[0]["urls"], ["https://relocate.dou.ua/jobs/?category=Python&from=maybe"])
            self.assertIsNone(calls[1]["urls"])


if __name__ == "__main__":
    unittest.main()
