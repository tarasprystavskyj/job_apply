from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruiter_response_scan import (  # noqa: E402
    RecruiterResponse,
    already_auto_replied,
    inbox_thread_urls,
    message_digest,
    plan_auto_reply,
    run_auto_replies,
)


class RecruiterAutoReplyPolicyTest(unittest.TestCase):
    def test_rejection_thank_you_is_high_confidence(self) -> None:
        confidence, intent, message, reason = plan_auto_reply(
            status="rejected",
            body="Unfortunately, we cannot move forward with your application.",
            recruiter_message="Unfortunately, we cannot move forward.",
            company="Example",
            role="Senior Python Engineer",
            recruiter_name="",
            public_resume_links=[],
        )

        self.assertGreaterEqual(confidence, 0.8)
        self.assertEqual(intent, "thank_you_rejection")
        self.assertIn("Thank you", message)
        self.assertIn("low-risk", reason)

    def test_resume_link_reply_uses_allowlisted_public_link(self) -> None:
        link = "https://drive.google.com/file/d/example/view?usp=sharing"
        confidence, intent, message, _reason = plan_auto_reply(
            status="positive_or_action_needed",
            body="Could you please send your CV or resume link?",
            recruiter_message="send your CV",
            company="Example",
            role="AI Engineer",
            recruiter_name="Recruiter",
            public_resume_links=[link],
        )

        self.assertGreaterEqual(confidence, 0.8)
        self.assertEqual(intent, "send_public_resume_link")
        self.assertIn(link, message)
        self.assertIn("LinkedIn", message)

    def test_scheduling_or_salary_requests_stay_manual(self) -> None:
        confidence, intent, message, reason = plan_auto_reply(
            status="positive_or_action_needed",
            body="Please send your CV and salary expectations, then let's schedule a call.",
            recruiter_message="salary expectations and call",
            company="Example",
            role="AI Engineer",
            recruiter_name="",
            public_resume_links=["https://drive.google.com/file/d/example/view?usp=sharing"],
        )

        self.assertLess(confidence, 0.8)
        self.assertEqual(intent, "manual_positive_action_needed")
        self.assertEqual(message, "")
        self.assertIn("scheduling", reason)

    def test_site_salary_warning_does_not_block_plain_cv_request(self) -> None:
        link = "https://drive.google.com/file/d/example/view?usp=sharing"
        confidence, intent, message, _reason = plan_auto_reply(
            status="review",
            body="Djinni page warning: profile salary is $5000 while application expectation is $3000.",
            recruiter_message="Hi Taras, Could you share your CV, please? Thank you, Irina",
            company="Seeking Alpha",
            role="Senior Python AI Engineer",
            recruiter_name="Iryna",
            public_resume_links=[link],
        )

        self.assertGreaterEqual(confidence, 0.8)
        self.assertEqual(intent, "send_public_resume_link")
        self.assertIn(link, message)

    def test_inbox_thread_urls_are_loaded_from_offer_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offers.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"thread_url": "https://djinni.co/my/inbox/25880123/#last"}),
                        json.dumps({"source_url": "https://djinni.co/jobs/1/"}),
                        json.dumps({"thread_url": "https://djinni.co/my/inbox/25880123/#last"}),
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(inbox_thread_urls(path), ["https://djinni.co/my/inbox/25880123/#last"])

    def test_sent_auto_reply_is_deduplicated_by_message_digest(self) -> None:
        class Row:
            thread_url = "https://djinni.co/my/inbox/1/#last"
            auto_reply_intent = "thank_you_rejection"
            suggested_auto_reply = "Thanks"

        existing = [
            {
                "thread_url": Row.thread_url,
                "auto_reply_intent": Row.auto_reply_intent,
                "message_digest": message_digest(Row.suggested_auto_reply),
                "sent": True,
            }
        ]

        self.assertTrue(already_auto_replied(existing, Row()))

    def test_auto_reply_dry_run_does_not_touch_browser(self) -> None:
        row = RecruiterResponse(
            schema="job.recruiter_response.v0",
            observed_at="2026-06-11T00:00:00+0300",
            source_site="djinni",
            thread_url="https://djinni.co/my/inbox/1/#last",
            company="Example",
            role="Python Engineer",
            status="rejected",
            recruiter_message="Unfortunately, no.",
            evidence="Unfortunately, no.",
            likely_lessons=[],
            option_a="A",
            option_b="B",
            suggested_thank_you_reply="Thanks",
            confidence=0.92,
            auto_reply_intent="thank_you_rejection",
            auto_reply_allowed=True,
            auto_reply_reason="clear rejection",
            suggested_auto_reply="Thanks",
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "auto.jsonl"
            with patch("recruiter_response_scan.prepare_or_send_thank_you") as prepare:
                events = run_auto_replies(
                    [row],
                    execute_send=False,
                    enabled=True,
                    threshold=0.8,
                    log_path=log_path,
                )

        prepare.assert_not_called()
        self.assertEqual(events[0]["result"], "dry_run_candidate")


if __name__ == "__main__":
    unittest.main()
