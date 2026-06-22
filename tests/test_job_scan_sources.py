from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import job_scan_sources  # noqa: E402


class JobScanSourcesTests(unittest.TestCase):
    def test_dou_scan_tries_description_and_broader_fallbacks(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run_dou_once(query_text: str, **kwargs: object) -> dict[str, object]:
            calls.append({"query": query_text, **kwargs})
            if kwargs.get("urls"):
                return {
                    "query": query_text,
                    "urls": kwargs["urls"],
                    "observations": 0,
                    "listing_remediations": {"exact_urls_extracted": 0, "blocked": 1},
                }
            if query_text == "Python AI":
                return {
                    "query": query_text,
                    "urls": ["https://jobs.dou.ua/vacancies/?search=Python+AI"],
                    "observations": 2,
                    "listing_remediations": {"exact_urls_extracted": 2, "blocked": 0},
                }
            return {
                "query": query_text,
                "urls": [f"https://jobs.dou.ua/vacancies/?search={query_text}"],
                "observations": 0,
                "listing_remediations": {"exact_urls_extracted": 0, "blocked": 1},
            }

        with patch.object(job_scan_sources, "run_dou_once", side_effect=fake_run_dou_once):
            summary = job_scan_sources.run_dou_with_fallbacks("Python AI automation", limit=10, max_pages=1)

        self.assertEqual(summary["query"], "Python AI")
        self.assertEqual(summary["observations"], 2)
        self.assertGreaterEqual(calls[0]["max_pages"], 2)
        self.assertTrue(any(call.get("urls") for call in calls))
        self.assertEqual([item["kind"] for item in summary["fallback_attempts"]], ["primary", "description_search", "broader_query"])

    def test_format_scan_summary_includes_djinni_unread_count(self) -> None:
        summary = {
            "batch": "candidate.csv",
            "notes": {"djinni_inbox": {"unread_count": 2}, "dou": {}},
            "errors": {},
        }

        text = job_scan_sources.format_scan_summary(summary)

        self.assertIn("djinni_inbox=ok unread=2", text)
        self.assertIn("dou=ok", text)


if __name__ == "__main__":
    unittest.main()
